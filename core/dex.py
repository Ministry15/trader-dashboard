"""Interface para DEXs do tipo Uniswap-V2 na BSC (PancakeSwap V2, BiSwap, ApeSwap).

Permite obter cotações on-chain via ``getAmountsOut`` e construir transacções
de swap (``swapExactTokensForTokens``) já com protecção de slippage. O envio
efectivo passa pela :class:`core.wallet.Wallet`, que respeita o modo dry-run.

Os DEXs V3 (ex.: PancakeSwap V3) usam um Quoter diferente e **não** são
suportados por esta classe — ``Dex`` aceita apenas ``type: uniswap_v2``.
"""
from __future__ import annotations

import logging
import time
from decimal import Decimal

from web3 import Web3

from core.wallet import Wallet
from utils.config import get_settings

logger = logging.getLogger(__name__)

# ABI mínima de um router Uniswap-V2.
ROUTER_V2_ABI = [
    {"name": "getAmountsOut", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "amountIn", "type": "uint256"},
                {"name": "path", "type": "address[]"}],
     "outputs": [{"name": "amounts", "type": "uint256[]"}]},
    {"name": "WETH", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "address"}]},
    {"name": "factory", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "address"}]},
    {"name": "swapExactTokensForTokens", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "amountIn", "type": "uint256"},
                {"name": "amountOutMin", "type": "uint256"},
                {"name": "path", "type": "address[]"},
                {"name": "to", "type": "address"},
                {"name": "deadline", "type": "uint256"}],
     "outputs": [{"name": "amounts", "type": "uint256[]"}]},
]

# ABI mínima da factory (só getPair).
FACTORY_ABI = [
    {"name": "getPair", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "tokenA", "type": "address"},
                {"name": "tokenB", "type": "address"}],
     "outputs": [{"name": "pair", "type": "address"}]},
]

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


class Dex:
    """Router de um DEX Uniswap-V2 na BSC."""

    def __init__(self, name: str, wallet: Wallet | None = None,
                 settings: dict | None = None):
        self.settings = settings or get_settings()
        cfg = self.settings["dexes"].get(name)
        if cfg is None:
            raise KeyError(f"DEX desconhecido: {name}")
        if cfg.get("type") != "uniswap_v2":
            raise ValueError(
                f"{name} é '{cfg.get('type')}'. Dex só suporta uniswap_v2."
            )
        self.name = name
        self.cfg = cfg
        self.wallet = wallet or Wallet(self.settings)
        self.w3 = self.wallet.w3
        self.router = self.w3.eth.contract(
            address=Web3.to_checksum_address(cfg["router"]), abi=ROUTER_V2_ABI
        )
        self.fee_bps = int(cfg.get("fee_bps", 0))

    # --------------------------------------------------------------- tokens/path
    def _token_meta(self, token: str):
        """Devolve (endereço_checksum, decimals) para símbolo conhecido ou endereço."""
        toks = self.settings["tokens"]
        if token in toks:
            return Web3.to_checksum_address(toks[token]["address"]), int(toks[token]["decimals"])
        addr = Web3.to_checksum_address(token)
        _, decimals = self.wallet._token_contract(addr)  # reaproveita resolução on-chain
        return addr, decimals

    def _wbnb(self) -> str:
        return Web3.to_checksum_address(self.settings["tokens"]["WBNB"]["address"])

    def _build_path(self, token_in: str, token_out: str) -> list[str]:
        """Caminho directo se existir par, ou via WBNB como intermediário."""
        a_in, _ = self._token_meta(token_in)
        a_out, _ = self._token_meta(token_out)
        wbnb = self._wbnb()
        if a_in == wbnb or a_out == wbnb:
            return [a_in, a_out]
        # Par directo (ex: USDT/token) — preferir se existir
        if self.has_liquidity(token_in, token_out):
            return [a_in, a_out]
        return [a_in, wbnb, a_out]

    # ----------------------------------------------------------------- liquidez
    def get_pair(self, token_a: str, token_b: str) -> str:
        """Endereço da pool ``token_a``/``token_b`` (ZERO_ADDRESS se não existir)."""
        if not hasattr(self, "_factory"):
            self._factory = self.w3.eth.contract(
                address=Web3.to_checksum_address(self.cfg["factory"]), abi=FACTORY_ABI)
        a, _ = self._token_meta(token_a)
        b, _ = self._token_meta(token_b)
        return self._factory.functions.getPair(a, b).call()

    def has_liquidity(self, token_a: str, token_b: str) -> bool:
        """True se existir pool para o par."""
        pair = self.get_pair(token_a, token_b)
        return bool(pair) and int(pair, 16) != 0

    def price_impact_bps(self, token_in: str, token_out: str, amount_in) -> Decimal:
        """Impacto de preço (bps) de ``amount_in`` vs uma cotação marginal pequena."""
        amount_in = Decimal(str(amount_in))
        if amount_in <= 0:
            return Decimal(0)
        tiny = amount_in / Decimal(1000)
        marginal = self.quote(token_in, token_out, tiny) / tiny       # out por unidade (marginal)
        executed = self.quote(token_in, token_out, amount_in) / amount_in  # out por unidade (real)
        if marginal <= 0:
            return Decimal(0)
        return (marginal - executed) / marginal * Decimal(10_000)

    # ------------------------------------------------------------------ cotações
    def get_amounts_out(self, amount_in_wei: int, path: list[str]) -> list[int]:
        return self.router.functions.getAmountsOut(int(amount_in_wei), path).call()

    def quote(self, token_in: str, token_out: str,
              amount_in: Decimal | float | str = Decimal(1)) -> Decimal:
        """Quantidade de ``token_out`` recebida por ``amount_in`` de ``token_in`` (unidades humanas)."""
        amount_in = Decimal(str(amount_in))
        a_in, dec_in = self._token_meta(token_in)
        a_out, dec_out = self._token_meta(token_out)
        path = self._build_path(token_in, token_out)
        amount_in_wei = int(amount_in * (Decimal(10) ** dec_in))
        out_wei = self.get_amounts_out(amount_in_wei, path)[-1]
        return Decimal(out_wei) / (Decimal(10) ** dec_out)

    def get_price(self, token_in: str, token_out: str) -> Decimal:
        """Preço de 1 unidade de ``token_in`` expresso em ``token_out``."""
        return self.quote(token_in, token_out, Decimal(1))

    # -------------------------------------------------------------------- swaps
    def build_swap_tx(self, token_in: str, token_out: str,
                      amount_in: Decimal | float | str, *,
                      slippage_bps: int | None = None,
                      recipient: str | None = None,
                      deadline_secs: int = 120) -> dict:
        """Constrói (mas não envia) a tx de swap com protecção de slippage.

        O resultado destina-se a :meth:`core.wallet.Wallet.send_transaction`,
        que aplica o gate de dry-run.
        """
        amount_in = Decimal(str(amount_in))
        a_in, dec_in = self._token_meta(token_in)
        path = self._build_path(token_in, token_out)
        amount_in_wei = int(amount_in * (Decimal(10) ** dec_in))

        amounts = self.get_amounts_out(amount_in_wei, path)
        out_wei = amounts[-1]
        if slippage_bps is None:
            slippage_bps = int(self.settings["trading"]["slippage_tolerance_bps"])
        min_out = (out_wei * (10_000 - slippage_bps)) // 10_000

        deadline = int(time.time()) + int(deadline_secs)
        to = Web3.to_checksum_address(recipient or self.wallet.address)

        gas_cfg = self.settings["trading"]["gas"]
        gas_price = self.w3.to_wei(gas_cfg["max_gas_price_gwei"], "gwei")
        tx = self.router.functions.swapExactTokensForTokens(
            amount_in_wei, int(min_out), path, to, deadline
        ).build_transaction({
            "from": self.wallet.address,
            "chainId": self.wallet.chain_id,
            "gas": int(gas_cfg["gas_limit"]),
            "gasPrice": gas_price,
            "nonce": self.w3.eth.get_transaction_count(self.wallet.address),
        })
        logger.info("[%s] swap construída: %s %s -> %s (min_out_wei=%s)",
                    self.name, amount_in, token_in, token_out, min_out)
        return tx


def load_v2_dexes(wallet: Wallet | None = None, settings: dict | None = None) -> dict[str, "Dex"]:
    """Instancia todos os DEXs uniswap_v2 definidos no settings.yaml."""
    settings = settings or get_settings()
    wallet = wallet or Wallet(settings)
    out = {}
    for name, cfg in settings["dexes"].items():
        if cfg.get("type") == "uniswap_v2":
            out[name] = Dex(name, wallet=wallet, settings=settings)
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    dexes = load_v2_dexes()
    print("DEXs uniswap_v2 carregados:", list(dexes.keys()))
    for name, dex in dexes.items():
        try:
            price = dex.get_price("WBNB", "USDT")
            print(f"{name:16s} 1 WBNB = {price:.4f} USDT  (fee {dex.fee_bps} bps)")
        except Exception as exc:  # noqa: BLE001
            print(f"{name:16s} erro -> {exc}")
    print("SMOKE OK")
