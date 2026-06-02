"""Gestão da carteira BSC: ligação web3, saldos e envio de transacções.

Respeita **sempre** o modo ``DRY_RUN``: em dry-run nenhuma transacção é
assinada nem enviada para a rede. A private key é lida do `.env` e nunca é
registada em log.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from eth_account import Account
from web3 import Web3
from web3.middleware import geth_poa_middleware

from utils.config import get_env, get_settings

logger = logging.getLogger(__name__)

MAX_UINT256 = (1 << 256) - 1  # aprovação "infinita" (padrão dos DEXs)

# ABI mínima ERC-20 — apenas o necessário para saldos e aprovações.
ERC20_ABI = [
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}],
     "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}],
     "type": "function"},
    {"constant": True, "inputs": [], "name": "decimals",
     "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "symbol",
     "outputs": [{"name": "", "type": "string"}], "type": "function"},
    {"constant": True,
     "inputs": [{"name": "_owner", "type": "address"},
                {"name": "_spender", "type": "address"}],
     "name": "allowance", "outputs": [{"name": "", "type": "uint256"}],
     "type": "function"},
    {"constant": False,
     "inputs": [{"name": "_spender", "type": "address"},
                {"name": "_value", "type": "uint256"}],
     "name": "approve", "outputs": [{"name": "", "type": "bool"}],
     "type": "function"},
]


class Wallet:
    """Carteira BSC com ligação web3, consulta de saldos e envio de tx."""

    def __init__(self, settings: dict | None = None):
        self.settings = settings or get_settings()
        net = self.settings["network"]
        self.chain_id = int(net["chain_id"])
        self._rpcs = [net.get("rpc_url"), net.get("rpc_url_backup")]
        self.w3 = self._connect()

        pk = get_env("BSC_PRIVATE_KEY")
        if not pk or "YOUR_" in pk or "_HERE" in pk:
            raise ValueError("BSC_PRIVATE_KEY ausente ou placeholder no .env")
        self.account = Account.from_key(pk if pk.startswith("0x") else "0x" + pk)
        self.address = Web3.to_checksum_address(self.account.address)

        self._dry_run = self._resolve_dry_run()
        logger.info("Wallet pronta: %s | dry_run=%s", self.address, self._dry_run)

    # ------------------------------------------------------------------ ligação
    def _connect(self) -> Web3:
        last_err = None
        for url in [u for u in self._rpcs if u]:
            try:
                w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 15}))
                # BSC é Proof-of-Authority -> precisa do middleware POA.
                w3.middleware_onion.inject(geth_poa_middleware, layer=0)
                if w3.is_connected():
                    logger.info("Ligado ao RPC BSC: %s", url)
                    return w3
            except Exception as exc:  # noqa: BLE001 - reportar e tentar backup
                last_err = exc
                logger.warning("Falha ao ligar a %s: %s", url, exc)
        raise ConnectionError(f"Sem ligação a nenhum RPC BSC ({last_err})")

    def _resolve_dry_run(self) -> bool:
        """Fail-safe: dry-run activo se QUALQUER fonte o indicar."""
        env = (get_env("DRY_RUN", "true") or "").strip().lower() in ("1", "true", "yes", "on")
        cfg = bool(self.settings.get("trading", {}).get("dry_run", True))
        return env or cfg

    # --------------------------------------------------------------------- info
    @property
    def dry_run(self) -> bool:
        return self._dry_run

    def is_connected(self) -> bool:
        return self.w3.is_connected()

    def get_bnb_balance(self) -> Decimal:
        """Saldo de BNB nativo (em BNB, não em wei)."""
        wei = self.w3.eth.get_balance(self.address)
        return Decimal(str(self.w3.from_wei(wei, "ether")))

    def _token_contract(self, token: str):
        """Resolve um token por símbolo (settings) ou endereço; devolve (contract, decimals)."""
        toks = self.settings["tokens"]
        if token in toks:
            addr = toks[token]["address"]
            decimals = int(toks[token]["decimals"])
        else:
            addr = token
            decimals = None
        contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(addr), abi=ERC20_ABI
        )
        if decimals is None:
            decimals = contract.functions.decimals().call()
        return contract, decimals

    def get_token_balance(self, token: str) -> Decimal:
        """Saldo de um token ERC-20 (símbolo conhecido ou endereço), já em unidades humanas."""
        contract, decimals = self._token_contract(token)
        raw = contract.functions.balanceOf(self.address).call()
        return Decimal(raw) / (Decimal(10) ** decimals)

    def get_allowance(self, token: str, spender: str) -> Decimal:
        contract, decimals = self._token_contract(token)
        raw = contract.functions.allowance(
            self.address, Web3.to_checksum_address(spender)
        ).call()
        return Decimal(raw) / (Decimal(10) ** decimals)

    # ----------------------------------------------------------------- aprovações
    def build_approve_tx(self, token: str, spender: str, amount_wei: int) -> dict:
        """Constrói (não envia) uma tx ERC-20 ``approve(spender, amount)``."""
        contract, _ = self._token_contract(token)
        gas_cfg = self.settings.get("trading", {}).get("gas", {})
        gas_price = self.w3.to_wei(gas_cfg.get("max_gas_price_gwei", 5), "gwei")
        return contract.functions.approve(
            Web3.to_checksum_address(spender), int(amount_wei)
        ).build_transaction({
            "from": self.address,
            "chainId": self.chain_id,
            "gas": 60000,
            "gasPrice": gas_price,
            "nonce": self.w3.eth.get_transaction_count(self.address),
        })

    def ensure_allowance(self, token: str, spender: str, min_amount,
                         *, approve_max: bool = True) -> dict:
        """Garante que ``spender`` tem allowance >= ``min_amount`` sobre ``token``.

        Se a allowance for suficiente, não faz nada. Caso contrário constrói e
        envia um ``approve`` (gated por dry-run — em dry-run não toca na rede).
        Por omissão aprova o máximo (``approve_max``), padrão dos routers DEX.
        """
        contract, decimals = self._token_contract(token)
        min_wei = int(Decimal(str(min_amount)) * (Decimal(10) ** decimals))
        spender = Web3.to_checksum_address(spender)
        current = contract.functions.allowance(self.address, spender).call()
        if current >= min_wei:
            return {"action": "none", "allowance_wei": current, "needed_wei": min_wei}

        amount = MAX_UINT256 if approve_max else min_wei
        logger.info("Allowance insuficiente p/ %s (%s < %s) — a aprovar%s",
                    token, current, min_wei, " (máx)" if approve_max else "")
        tx = self.build_approve_tx(token, spender, amount)
        result = self.send_transaction(tx)
        return {"action": "approve", "needed_wei": min_wei, "sent": result}

    # ------------------------------------------------------------------- escrita
    def send_transaction(self, tx: dict) -> dict:
        """Assina e envia uma transacção — bloqueada em modo dry-run.

        Em dry-run devolve ``{"dry_run": True, ...}`` sem tocar na rede.
        """
        tx = dict(tx)
        tx.setdefault("chainId", self.chain_id)
        tx.setdefault("from", self.address)
        if "nonce" not in tx:
            tx["nonce"] = self.w3.eth.get_transaction_count(self.address)

        if self._dry_run:
            safe = {k: tx.get(k) for k in ("to", "value", "gas", "gasPrice", "nonce")}
            logger.warning("[DRY_RUN] transacção NÃO enviada: %s", safe)
            return {"dry_run": True, "tx": tx}

        signed = self.account.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed.rawTransaction)
        logger.info("Transacção enviada: %s", tx_hash.hex())
        return {"dry_run": False, "tx_hash": tx_hash.hex()}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    w = Wallet()
    print("Endereço:        ", w.address)
    print("Ligado:          ", w.is_connected())
    print("Chain ID:        ", w.chain_id)
    print("Dry-run:         ", w.dry_run)
    print("Saldo BNB:       ", w.get_bnb_balance())
    for sym in ("WBNB", "USDT", "CAKE"):
        try:
            print(f"Saldo {sym}:".ljust(17), w.get_token_balance(sym))
        except Exception as exc:  # noqa: BLE001
            print(f"Saldo {sym}: erro -> {exc}")
    print("SMOKE OK")
