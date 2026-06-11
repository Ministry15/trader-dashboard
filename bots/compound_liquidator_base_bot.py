"""Bot de liquidações Compound V3 (Comet USDC) na chain Base.

Lógica diferente do Aave — usa o modelo Compound V3 (Comet):
  1. Mantém lista de borrowers via eventos Withdraw do Comet
  2. Para cada borrower:
       a. borrowBalanceOf(addr) → dívida em USDC
       b. isLiquidatable(addr)  → bool
       c. Se True: calcula colateral via collateralBalanceOf + getPrice
       d. Estima lucro = colateral × desconto (~5% conservador)
  3. DRY_RUN=true: regista oportunidade na BD, NÃO executa
  4. DRY_RUN=false: chama absorb() no Comet para liquidar
       (absorb é open — não requer flash loan)

Diferenças vs Aave V3:
  - Um único contrato (Comet) em vez de Pool + Oracle
  - isLiquidatable() → bool em vez de healthFactor
  - absorb(absorber, accounts[]) em vez de liquidationCall
  - Lucro via buyCollateral() com desconto do protocolo
  - Health factor calculado: sum(col_i × liquidateCF_i) / debt_usd

Contrato Compound V3 USDC Comet na Base (chain 8453):
  Comet: 0xb125E6687d4313864e53df431d5425969c15Eb2F
  BaseToken: USDC (0x833589fcd6edb6e08f4c7c32d4f71b54bda02913)
  5 collateral assets: cbETH, WETH, wstETH, cbBTC, LBTC

RPC: ALCHEMY_BASE_URL do .env (fallback: https://base-rpc.publicnode.com)
"""
from __future__ import annotations

import asyncio
import json
import queue
import threading
import websockets
import time
from dataclasses import dataclass, field

import requests
from web3 import Web3
from web3.exceptions import ContractLogicError

from utils.config import get_env, get_settings
from utils.database import init_db, upsert_liquidation_opportunity
from utils.flashbots import send_bundle as _fb_send_bundle
from utils.flashbots import send_bundle_multi as _fb_send_multi
from utils.logger import get_logger
from utils.notifier import TelegramNotifier

logger = get_logger(__name__)

_MAX_BUNDLE_TXS = 4

# ── Compound V3 Base — endereços ──────────────────────────────────────────────

COMET_ADDRESS     = Web3.to_checksum_address("0xb125E6687d4313864e53df431d5425969c15Eb2F")
BASE_CHAIN_ID     = 8453
USDC_DECIMALS     = 6          # USDC tem 6 decimais na Base
_PRICE_DECIMALS   = 8          # getPrice retorna USD × 1e8
_CF_SCALE         = 1e18       # borrowCollateralFactor / liquidateCollateralFactor em 1e18
_GAS_UNITS        = 500_000    # absorb() + buyCollateral() estimativa
_ABSORB_DISCOUNT  = 0.05       # desconto conservador de 5% no buyCollateral
_BASE_FALLBACK_RPC  = "https://mainnet.base.org"
_BASE_EXTRA_RPCS    = [
    "https://1rpc.io/base",
    "https://mainnet.base.org",
]
_BASE_WSS_PRIMARY  = "wss://base.publicnode.com"
_BASE_WSS_FALLBACK = "wss://base.drpc.org"

_SCAN_INTERVAL_BLOCKS = 60   # ~2 min em Base (2s/bloco)

_HF_LIQUIDATABLE = 1.0

_FLASHBOTS_ENDPOINT      = "https://relay.flashbots.net"
_FLASHBOTS_MIN_PROFIT_USD = 500.0
_BLACKLIST_FAILS  = 3

# ── ABI mínimo do Comet ───────────────────────────────────────────────────────

_COMET_ABI = [
    # ── view ──────────────────────────────────────────────────────────────────
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "isLiquidatable",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "borrowBalanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "numAssets",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "i", "type": "uint8"}],
        "name": "getAssetInfo",
        "outputs": [
            {
                "components": [
                    {"name": "offset",                  "type": "uint8"},
                    {"name": "asset",                   "type": "address"},
                    {"name": "priceFeed",               "type": "address"},
                    {"name": "scale",                   "type": "uint64"},
                    {"name": "borrowCollateralFactor",  "type": "uint64"},
                    {"name": "liquidateCollateralFactor","type": "uint64"},
                    {"name": "liquidationFactor",       "type": "uint64"},
                    {"name": "supplyCap",               "type": "uint128"},
                ],
                "name": "",
                "type": "tuple",
            }
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "priceFeed", "type": "address"}],
        "name": "getPrice",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "baseTokenPriceFeed",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "asset",   "type": "address"},
        ],
        "name": "collateralBalanceOf",
        "outputs": [{"name": "", "type": "uint128"}],
        "stateMutability": "view",
        "type": "function",
    },
    # ── write (só usado em live) ───────────────────────────────────────────────
    {
        "inputs": [
            {"name": "absorber",  "type": "address"},
            {"name": "accounts",  "type": "address[]"},
        ],
        "name": "absorb",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    # ── eventos ───────────────────────────────────────────────────────────────
    {
        # Withdraw(address indexed src, address indexed to, uint256 amount)
        # topic: 0x9b1bfa7fa9ee420a16e124f794c35ac9f90472acc99140eb2f6447c714cad8eb
        "anonymous": False,
        "inputs": [
            {"indexed": True,  "name": "src",    "type": "address"},
            {"indexed": True,  "name": "to",     "type": "address"},
            {"indexed": False, "name": "amount", "type": "uint256"},
        ],
        "name": "Withdraw",
        "type": "event",
    },
    {
        # Supply(address indexed from, address indexed dst, uint256 amount)
        # topic: 0xd1cf3d156d5f8f0d50f6c122ed609cec09d35c9b9fb3fff6ea0959134dae424e
        "anonymous": False,
        "inputs": [
            {"indexed": True,  "name": "from",   "type": "address"},
            {"indexed": True,  "name": "dst",    "type": "address"},
            {"indexed": False, "name": "amount", "type": "uint256"},
        ],
        "name": "Supply",
        "type": "event",
    },
    {
        # AbsorbDebt(address indexed absorber, address indexed borrower,
        #            uint256 basePaidOut, uint256 usdValue)
        # topic: 0x1547a878dc89ad3c367b6338b4be6a65a5dd74fb77ae044da1e8747ef1f4f62f
        "anonymous": False,
        "inputs": [
            {"indexed": True,  "name": "absorber",   "type": "address"},
            {"indexed": True,  "name": "borrower",   "type": "address"},
            {"indexed": False, "name": "basePaidOut", "type": "uint256"},
            {"indexed": False, "name": "usdValue",   "type": "uint256"},
        ],
        "name": "AbsorbDebt",
        "type": "event",
    },
]


@dataclass
class LiqOpportunityCompound:
    borrower: str
    health_factor: float
    is_liquidatable: bool
    total_collateral_usd: float
    total_debt_usd: float
    estimated_profit_usd: float
    gas_cost_usd: float
    liquidation_bonus_pct: float
    best_collateral_asset: str = field(default="")


# ── Bot ───────────────────────────────────────────────────────────────────────

class CompoundLiquidatorBaseBot:
    """Monitoriza e (em modo live) executa liquidações Compound V3 USDC na Base."""

    def __init__(self, settings: dict | None = None):
        self.settings = settings or get_settings()
        self.cfg = self.settings.get("bots", {}).get("compound_liquidator_base", {})

        primary_rpc = get_env("ALCHEMY_BASE_URL") or _BASE_FALLBACK_RPC
        _seen = {primary_rpc, _BASE_FALLBACK_RPC}
        self._rpc_urls: list[str] = [primary_rpc, _BASE_FALLBACK_RPC] + [
            u for u in _BASE_EXTRA_RPCS if u not in _seen
        ]
        self._active_rpc: str = primary_rpc

        self.dry_run      : bool  = False
        self.hf_threshold : float = float(self.cfg.get("health_factor_threshold", 1.2))
        self.min_profit   : float = float(self.cfg.get("min_profit_usd", 8.0))
        self.scan_blocks  : int   = int(self.cfg.get("borrower_scan_blocks", 200_000))
        self.max_per_tick : int   = int(self.cfg.get("max_positions_per_tick", 50))

        self.w3    = Web3(Web3.HTTPProvider(primary_rpc, request_kwargs={"timeout": 30}))
        self.comet = self.w3.eth.contract(address=COMET_ADDRESS, abi=_COMET_ABI)

        self._borrowers   : set[str] = set()
        self._scan_from   : int = 0
        self._assets      : list[dict] = []   # cache de asset info + preços
        self._assets_ts   : float = 0.0
        self._eth_price_cache: float = 2000.0
        self._eth_price_ts   : float = 0.0
        self._cooldown       : dict[str, float] = {}
        self._fail_counts    : dict[str, int]   = {}
        self._blacklist      : dict[str, float] = {}

        self._block_queue:  queue.Queue = queue.Queue(maxsize=20)
        self._last_block:   int   = 0
        self._ws_last_seen: float = time.time()
        self._ws_stop       = threading.Event()
        self._ws_thread     = threading.Thread(
            target=self._ws_runner, daemon=True, name="cmpd-base-ws")
        self._ws_thread.start()

        self.notifier = TelegramNotifier(self.settings)
        init_db()

        logger.info(
            "CompoundBase: rpc=%s dry_run=%s hf<%.2f min_profit=$%.2f",
            primary_rpc.split("//")[-1].split("/")[0], self.dry_run,
            self.hf_threshold, self.min_profit,
        )

    # ── helpers ──────────────────────────────────────────────────────────────

    def _switch_rpc(self, failed_url: str) -> bool:
        for url in self._rpc_urls:
            if url == failed_url:
                continue
            try:
                test_w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 15}))
                test_w3.eth.block_number
                self.w3    = test_w3
                self._active_rpc = url
                self.comet = self.w3.eth.contract(address=COMET_ADDRESS, abi=_COMET_ABI)
                logger.warning("CompoundBase: RPC trocado para fallback: %s",
                               url.split("//")[-1].split("/")[0])
                return True
            except Exception:
                continue
        return False

    def _connected(self) -> bool:
        for attempt in range(3):
            try:
                self.w3.eth.block_number
                return True
            except requests.exceptions.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else 0
                if status == 429:
                    if attempt == 0:
                        logger.warning(
                            "CompoundBase: rate-limit (429) — a tentar fallback RPC"
                        )
                        if self._switch_rpc(self._active_rpc):
                            continue
                    time.sleep(2 ** attempt)
                else:
                    logger.debug("CompoundBase: HTTP %s ao verificar ligação: %s", status, exc)
                    if attempt == 0 and self._active_rpc != _BASE_FALLBACK_RPC:
                        if self._switch_rpc(self._active_rpc):
                            continue
                    return False
            except Exception as exc:
                logger.debug("CompoundBase: erro ao verificar ligação RPC: %s", exc)
                if attempt == 0 and self._active_rpc != _BASE_FALLBACK_RPC:
                    if self._switch_rpc(self._active_rpc):
                        continue
                return False
        return False

    def _eth_price(self) -> float:
        """Preço ETH em USD via getPrice do Comet."""
        now = time.time()
        if now - self._eth_price_ts < 300:
            return self._eth_price_cache
        try:
            # WETH é asset[1] no Comet
            assets = self._load_assets()
            for a in assets:
                if a["asset"].lower() == "0x4200000000000000000000000000000000000006":
                    self._eth_price_cache = a["price_usd"]
                    self._eth_price_ts    = now
                    return self._eth_price_cache
        except Exception:
            pass
        return self._eth_price_cache

    def _gas_price_gwei(self) -> float:
        try:
            return self.w3.eth.gas_price / 1e9
        except Exception:
            return 0.01   # Base: muito barato (~0.001-0.01 Gwei)

    def _load_assets(self) -> list[dict]:
        """Cache de asset info com preços (TTL: 5 min). Tenta todos os RPCs em caso de 429."""
        now = time.time()
        if self._assets and now - self._assets_ts < 300:
            return self._assets
        rpcs_to_try = [self._active_rpc] + [u for u in self._rpc_urls if u != self._active_rpc]
        for rpc_url in rpcs_to_try:
            try:
                w3_tmp   = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 20}))
                comet_tmp = w3_tmp.eth.contract(address=COMET_ADDRESS, abi=_COMET_ABI)
                n = comet_tmp.functions.numAssets().call()
                assets = []
                for i in range(n):
                    info      = comet_tmp.functions.getAssetInfo(i).call()
                    price_raw = comet_tmp.functions.getPrice(info[2]).call()
                    assets.append({
                        "asset":          info[1],
                        "price_feed":     info[2],
                        "scale":          info[3],
                        "borrow_cf":      info[4] / _CF_SCALE,
                        "liquidate_cf":   info[5] / _CF_SCALE,
                        "liquidation_f":  info[6] / _CF_SCALE,
                        "price_usd":      price_raw / 10 ** _PRICE_DECIMALS,
                        "discount":       1.0 - info[6] / _CF_SCALE,
                    })
                self._assets    = assets
                self._assets_ts = now
                logger.debug("CompoundBase: %d assets carregados via %s",
                             len(assets), rpc_url.split("//")[-1].split("/")[0])
                return self._assets
            except Exception as exc:
                logger.warning("CompoundBase: falha ao carregar assets via %s: %s",
                               rpc_url.split("//")[-1].split("/")[0], exc)
        return self._assets

    # ── descoberta de borrowers ───────────────────────────────────────────────

    def _scan_borrowers(self) -> None:
        if not self._connected():
            return
        try:
            latest = self.w3.eth.block_number
        except Exception as exc:
            logger.warning("CompoundBase: não consegui ler bloco actual: %s", exc)
            return

        from_block = self._scan_from or max(0, latest - self.scan_blocks)
        scan_start = from_block
        chunk, added = 2000, 0

        while from_block < latest:
            to_block = min(from_block + chunk - 1, latest)
            try:
                # Withdraw(src, to, amount) — src é o borrower
                events = self.comet.events.Withdraw().get_logs(
                    from_block=from_block, to_block=to_block)
                for e in events:
                    addr = e.args["src"].lower()
                    if addr not in self._borrowers:
                        self._borrowers.add(addr)
                        added += 1
                from_block = to_block + 1
            except Exception as exc:
                logger.debug("CompoundBase: Withdraw events [%d..%d]: %s",
                             from_block, to_block, exc)
                break

        self._scan_from = latest

        # Remover posições já liquidadas (AbsorbDebt recentes)
        try:
            absorb_start = max(0, latest - 5_000)
            absorb_evts = self.comet.events.AbsorbDebt().get_logs(
                from_block=absorb_start, to_block=latest)
            for e in absorb_evts:
                self._borrowers.discard(e.args["borrower"].lower())
        except Exception:
            pass

        logger.info("CompoundBase: scan blocos %d..%d +%d novos borrowers (total=%d)",
                    scan_start, latest, added, len(self._borrowers))

    # ── análise de posições ───────────────────────────────────────────────────

    def _check_position(self, address: str) -> LiqOpportunityCompound | None:
        cs_addr = Web3.to_checksum_address(address)
        try:
            # 1. Dívida em USDC
            debt_raw = self.comet.functions.borrowBalanceOf(cs_addr).call()
            if debt_raw == 0:
                self._borrowers.discard(address)  # posição fechada
                return None
            debt_usd = debt_raw / 10 ** USDC_DECIMALS

            # 2. isLiquidatable
            is_liq = self.comet.functions.isLiquidatable(cs_addr).call()

            # 3. Colateral: só calcular para posições de interesse
            assets = self._load_assets()
            if not assets:
                return None

            collateral_usd      = 0.0
            liq_threshold_usd   = 0.0  # sum(col_i × liquidateCF_i)
            best_asset          = ""
            best_col_usd        = 0.0

            for a in assets:
                try:
                    bal = self.comet.functions.collateralBalanceOf(
                        cs_addr, Web3.to_checksum_address(a["asset"])).call()
                    if bal == 0:
                        continue
                    val_usd = (bal / a["scale"]) * a["price_usd"]
                    collateral_usd    += val_usd
                    liq_threshold_usd += val_usd * a["liquidate_cf"]
                    if val_usd > best_col_usd:
                        best_col_usd  = val_usd
                        best_asset    = a["asset"]
                except Exception:
                    pass

            # 4. Risk score estilo Aave: liq_threshold / debt
            #    > 1.0 = saudável, < 1.0 = liquidável, = 0.0 = sem colateral
            if liq_threshold_usd > 0 and debt_usd > 0:
                pseudo_hf = liq_threshold_usd / debt_usd
            elif is_liq:
                # Sem colateral mas marcado liquidável pelo protocolo — registar como HF=0
                pseudo_hf = 0.0
            else:
                # Sem colateral e NÃO liquidável: dívida fechada ou posição inválida
                self._borrowers.discard(address)
                return None

            # 5. Filtrar posições saudáveis (margem de segurança > threshold)
            if not is_liq and pseudo_hf >= self.hf_threshold:
                return None

            if not is_liq and pseudo_hf < _HF_LIQUIDATABLE:
                logger.warning(
                    "CompoundBase: DISCREPÂNCIA %s — HF local=%.4f<1.0 mas isLiquidatable()=False"
                    " debt=$%.2f col=$%.2f",
                    cs_addr[:10] + "…", pseudo_hf, debt_usd, collateral_usd,
                )

            # 6. Gas
            gas_usd = _GAS_UNITS * self._gas_price_gwei() * 1e-9 * self._eth_price()

            # 7. Lucro estimado — usa pseudo_hf < 1.0 como fallback se isLiquidatable() divergir
            effectively_liq = is_liq or pseudo_hf < 1.0
            if effectively_liq:
                net_profit = max(debt_usd * 0.08 - gas_usd, 0.0)
                bonus_pct  = 8.0
            else:
                net_profit = 0.0
                bonus_pct  = 0.0

            return LiqOpportunityCompound(
                borrower=address,
                health_factor=round(pseudo_hf, 6),
                is_liquidatable=effectively_liq,
                total_collateral_usd=round(collateral_usd, 2),
                total_debt_usd=round(debt_usd, 2),
                estimated_profit_usd=round(net_profit, 4),
                gas_cost_usd=round(gas_usd, 6),
                liquidation_bonus_pct=bonus_pct,
                best_collateral_asset=best_asset,
            )

        except (ContractLogicError, Exception) as exc:
            logger.debug("CompoundBase: posição %s…: %s", address[:10], exc)
            return None

    # ── execução / registo ────────────────────────────────────────────────────

    def _record(self, opp: LiqOpportunityCompound) -> None:
        status = "dry_run" if self.dry_run else ("liquidatable" if opp.is_liquidatable else "watching")
        rec_id, inserted = upsert_liquidation_opportunity(
            position_address=opp.borrower,
            health_factor=opp.health_factor,
            debt_asset="USDC",
            debt_amount_usd=opp.total_debt_usd,
            collateral_asset=opp.best_collateral_asset or "mixed",
            collateral_amount_usd=opp.total_collateral_usd,
            liquidation_bonus_pct=opp.liquidation_bonus_pct,
            estimated_profit_usd=opp.estimated_profit_usd,
            gas_cost_usd=opp.gas_cost_usd,
            executed=False,
            tx_hash=None,
            dry_run=self.dry_run,
            status=status,
            chain="compound_base",
        )
        action = "INSERT" if inserted else "UPDATE"
        logger.debug("CompoundBase: BD %s id=%d %s HF=%.4f liq=%s",
                     action, rec_id, opp.borrower[:10], opp.health_factor, opp.is_liquidatable)

    def _execute_live(self, opp: LiqOpportunityCompound, nonce: int) -> str | None:
        _b_low = opp.borrower.lower()
        try:
            pk   = get_env("BSC_PRIVATE_KEY") or ""
            acct = self.w3.eth.account.from_key(pk)
            # simulação usa 1rpc.io/base para evitar 429 no RPC principal
            _sim_rpcs = [_BASE_FALLBACK_RPC] + [u for u in self._rpc_urls if u != _BASE_FALLBACK_RPC]
            _sim_ok = False
            _last_sim_exc: Exception | None = None
            for _sim_url in _sim_rpcs:
                try:
                    _sim_w3    = Web3(Web3.HTTPProvider(_sim_url, request_kwargs={"timeout": 20}))
                    _sim_comet = _sim_w3.eth.contract(address=COMET_ADDRESS, abi=_COMET_ABI)
                    _sim_comet.functions.absorb(
                        acct.address,
                        [Web3.to_checksum_address(opp.borrower)],
                    ).call({"from": acct.address})
                    _sim_ok = True
                    break
                except Exception as _exc:
                    _last_sim_exc = _exc
                    if "429" not in str(_exc) and "Too Many" not in str(_exc):
                        break  # erro de revert — não adianta tentar outro RPC
            if not _sim_ok:
                self._cooldown[_b_low] = time.time() + 120
                logger.warning(
                    "CompoundBase: simulação falhou %s — cooldown 2min: %s",
                    opp.borrower[:10] + "…", _last_sim_exc,
                )
                return None
            tx = self.comet.functions.absorb(
                acct.address,
                [Web3.to_checksum_address(opp.borrower)],
            ).build_transaction({
                "from":     acct.address,
                "chainId":  BASE_CHAIN_ID,
                "gas":      _GAS_UNITS,
                "gasPrice": int(self.w3.eth.gas_price * 1.15),
                "nonce":    nonce,
            })
            signed = acct.sign_transaction(tx)
            if opp.estimated_profit_usd >= _FLASHBOTS_MIN_PROFIT_USD:
                try:
                    _tgt = self.w3.eth.block_number + 1
                    _bh = _fb_send_bundle(
                        "0x" + signed.raw_transaction.hex(),
                        _tgt, _FLASHBOTS_ENDPOINT, pk,
                    )
                    if _bh:
                        _exp = Web3.keccak(primitive=bytes(signed.raw_transaction))
                        logger.info("CompoundBase: TX via Flashbots @ bloco %d: %s…", _tgt, _exp.hex()[:18])
                        return _exp.hex()
                except Exception as _fb_exc:
                    logger.warning("CompoundBase: Flashbots falhou — fallback mempool: %s", _fb_exc)
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            logger.info("CompoundBase: ABSORB TX: %s", tx_hash.hex())
            return tx_hash.hex()
        except Exception as exc:
            self._cooldown[_b_low] = time.time() + 300
            logger.warning("CompoundBase: absorb revertida — cooldown 5min: %s", exc)
            return None

    # ── Multi-bundle (Phase 6) ───────────────────────────────────────────────
    def _try_bundle(self, opps: list, base_nonce: int) -> set[str]:
        """Compound V3: absorb([b1, b2, ...]) in ONE tx (more efficient than multi-tx bundle).
        Falls back to multi-tx bundle if multi-absorb fails."""
        if len(opps) < 2:
            return set()
        try:
            pk   = get_env("BSC_PRIVATE_KEY") or ""
            acct = self.w3.eth.account.from_key(pk)
            target = self.w3.eth.block_number + 1

            # Group by comet (each comet gets its own absorb call)
            comet_groups: dict = {}
            for opp in opps[:_MAX_BUNDLE_TXS]:
                comet_groups.setdefault(opp.comet_key, []).append(opp)

            raw_txes, keys = [], []
            nonce_off = 0
            for comet_key, group_opps in comet_groups.items():
                contract = self._contracts.get(comet_key)
                if not contract:
                    continue
                borrowers = [Web3.to_checksum_address(o.borrower) for o in group_opps]
                tx = contract.functions.absorb(
                    acct.address, borrowers,
                ).build_transaction({
                    "from":     acct.address,
                    "chainId":  BASE_CHAIN_ID,
                    "gas":      _GAS_UNITS * len(borrowers),
                    "gasPrice": int(self.w3.eth.gas_price * 1.15),
                    "nonce":    base_nonce + nonce_off,
                })
                signed = acct.sign_transaction(tx)
                raw_txes.append("0x" + signed.raw_transaction.hex())
                keys.extend(o.borrower.lower() for o in group_opps)
                nonce_off += 1

            if not raw_txes:
                return set()

            if len(raw_txes) == 1:
                # Single comet: use regular send_bundle
                from utils.flashbots import send_bundle as _fb
                bh = _fb(raw_txes[0], target, _FLASHBOTS_ENDPOINT, pk)
            else:
                bh = _fb_send_multi(raw_txes, target, _FLASHBOTS_ENDPOINT, pk)

            if bh:
                total = sum(len(g) for g in comet_groups.values())
                logger.info("CompoundBase: bundle %d borrowers / %d tx @ bloco %d: %s…",
                            total, len(raw_txes), target, bh[:16])
                return set(keys)
            return set()
        except Exception as exc:
            logger.warning("CompoundBase: bundle falhou → fallback individual: %s", exc)
            return set()

    # ── tick ─────────────────────────────────────────────────────────────────

    def _ws_runner(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._ws_listen())
        finally:
            loop.close()

    async def _ws_listen(self) -> None:
        wss_urls = [_BASE_WSS_PRIMARY, _BASE_WSS_FALLBACK]
        idx = 0
        while not self._ws_stop.is_set():
            url = wss_urls[idx % len(wss_urls)]
            try:
                async with websockets.connect(
                    url, ping_interval=20, ping_timeout=10, open_timeout=10
                ) as ws:
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0", "id": 1,
                        "method": "eth_subscribe", "params": ["newHeads"],
                    }))
                    sub = json.loads(await ws.recv())
                    logger.info("CompoundBase: WS newHeads subscrito @ %s (id=%s)",
                        url.split("//")[-1].split("/")[0], sub.get("result", "?")[:12])
                    while not self._ws_stop.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=10)
                            msg = json.loads(raw)
                            blk_hex = (msg.get("params") or {}).get("result", {}).get("number")
                            if blk_hex:
                                blk = int(blk_hex, 16)
                                self._ws_last_seen = time.time()
                                try:
                                    self._block_queue.put_nowait(blk)
                                except queue.Full:
                                    self._block_queue.get_nowait()
                                    self._block_queue.put_nowait(blk)
                        except asyncio.TimeoutError:
                            continue
                        except Exception:
                            break
            except Exception as exc:
                logger.warning("CompoundBase: WS erro @ %s — reconecta em 5s: %s",
                    url.split("//")[-1].split("/")[0], exc)
                idx += 1
            await asyncio.sleep(5)

    def tick(self) -> list[dict]:
        block_num = None
        try:
            while True:
                block_num = self._block_queue.get_nowait()
        except queue.Empty:
            pass
        if block_num is None:
            if time.time() - self._ws_last_seen > 30:
                try:
                    block_num = self.w3.eth.block_number
                except Exception:
                    return []
            else:
                return []
        if block_num <= self._last_block:
            return []
        self._last_block = block_num

        _now_ck = time.time()
        self._cooldown = {k: v for k, v in self._cooldown.items() if v > _now_ck}

        if block_num % _SCAN_INTERVAL_BLOCKS == 0 or not self._borrowers:
            self._scan_borrowers()
            self._load_assets()
        if not self._borrowers:
            return []

        _pk   = get_env("BSC_PRIVATE_KEY") or ""
        _acct = self.w3.eth.account.from_key(_pk)
        _nonce: int | None = None
        if not self.dry_run:
            try:
                _bal_wei = self.w3.eth.get_balance(_acct.address)
            except Exception as exc:
                logger.warning("CompoundBase: get_balance falhou — tick saltado: %s", exc)
                return []
            if _bal_wei < Web3.to_wei(0.005, 'ether'):
                logger.error(
                    "CompoundBase: saldo insuficiente (%.6f ETH < 0.005) — execução suspensa",
                    float(Web3.from_wei(_bal_wei, 'ether')),
                )
                return []

        candidates   = list(self._borrowers)[:self.max_per_tick]
        _now_tick    = time.time()
        logger.info("CompoundBase Tick: bloco=%d %d candidatos | %d blacklist",
                    block_num, len(candidates), len(self._blacklist))

        # ── Bundle attempt (Phase 6) ──────────────────────────────────────────
        bundled: set[str] = set()
        if not self.dry_run:
            # pre-check: collect liquidatable opps
            _bdl_candidates = []
            for _b in candidates:
                _o = self._check_position(_b)
                if (_o and _o.is_liquidatable
                        and _o.estimated_profit_usd >= self.min_profit
                        and self._cooldown.get(_b.lower(), 0) <= time.time()):
                    _bdl_candidates.append(_o)
            if len(_bdl_candidates) >= 2:
                if _nonce is None:
                    _nonce = self.w3.eth.get_transaction_count(_acct.address, 'pending')
                _bdl_sorted = sorted(_bdl_candidates, key=lambda o: -o.estimated_profit_usd)
                bundled = self._try_bundle(_bdl_sorted, _nonce)
                if bundled:
                    _nonce += len({o.borrower.lower() for o in _bdl_candidates
                                   if o.borrower.lower() in bundled})
                    logger.info("CompoundBase: %d borrowers via bundle Flashbots", len(bundled))

        results: list[dict] = []
        _liq_count = _watch_count = 0

        for borrower in candidates:
            _b_low = borrower.lower()

            opp = self._check_position(borrower)
            if opp is None:
                continue

            if _b_low in self._blacklist:
                _bl_hf = self._blacklist[_b_low]
                if abs(opp.health_factor - _bl_hf) / max(_bl_hf, 0.001) < 0.05:
                    logger.debug("CompoundBase: %s blacklisted — saltado",
                                 borrower[:10] + "…")
                    continue
                del self._blacklist[_b_low]
                self._fail_counts.pop(_b_low, None)
                logger.info("CompoundBase: %s saiu da blacklist", borrower[:10] + "…")

            tx_hash = None
            if opp.is_liquidatable:
                _liq_count += 1
                logger.info(
                    "CompoundBase: LIQUIDÁVEL %s HF=%.4f dívida=$%.2f col=$%.2f lucro≈$%.4f dry=%s",
                    borrower[:10] + "…", opp.health_factor, opp.total_debt_usd,
                    opp.total_collateral_usd, opp.estimated_profit_usd, self.dry_run,
                )
                if not self.dry_run and opp.estimated_profit_usd >= self.min_profit:
                    _until = self._cooldown.get(_b_low, 0)
                    if _until > _now_tick:
                        logger.debug("CompoundBase: %s em cooldown (%.0fs)",
                                     borrower[:10] + "…", _until - _now_tick)
                    elif _b_low in bundled:
                        tx_hash = "bundle"  # já enviado no bundle
                    else:
                        if _nonce is None:
                            _nonce = self.w3.eth.get_transaction_count(_acct.address, 'pending')
                        tx_hash = self._execute_live(opp, nonce=_nonce)
                        if tx_hash is None:
                            _cnt = self._fail_counts.get(_b_low, 0) + 1
                            self._fail_counts[_b_low] = _cnt
                            if _cnt >= _BLACKLIST_FAILS:
                                self._blacklist[_b_low] = opp.health_factor
                                logger.warning(
                                    "CompoundBase: %s adicionado à blacklist (%d falhas)",
                                    borrower[:10] + "…", _cnt,
                                )
                        else:
                            self._fail_counts.pop(_b_low, None)
                            _nonce += 1
                            self.notifier.notify(
                                "trade_executed",
                                f"🟢 Compound Base ABSORB {borrower[:10]}… "
                                f"lucro≈${opp.estimated_profit_usd:.2f} | tx={tx_hash[:20]}…",
                            )
            else:
                _watch_count += 1
                logger.debug(
                    "CompoundBase: vigiar %s HF=%.4f dívida=$%.2f",
                    borrower[:10] + "…", opp.health_factor, opp.total_debt_usd,
                )

            self._record(opp)
            results.append({
                "borrower":        borrower,
                "hf":              opp.health_factor,
                "is_liquidatable": opp.is_liquidatable,
                "debt_usd":        opp.total_debt_usd,
                "profit_usd":      opp.estimated_profit_usd,
                "executed":        tx_hash is not None,
                "dry_run":         self.dry_run,
            })

        logger.info(
            "CompoundBase: tick — %d liquidáveis / %d em vigilância (%d candidatos, %d borrowers)",
            _liq_count, _watch_count, len(candidates), len(self._borrowers),
        )
        return results


# ── Smoke test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging_mod = __import__("logging")
    logging_mod.basicConfig(
        level=logging_mod.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    print("=" * 60)
    print("COMPOUND V3 BASE LIQUIDATOR — Smoke Test")
    print("=" * 60)

    bot = CompoundLiquidatorBaseBot()

    connected = bot._connected()
    print(f"\n[1] RPC Base conectado: {connected}")
    if not connected:
        sys.exit(0)

    try:
        bloco = bot.w3.eth.block_number
        print(f"[2] Bloco actual Base: {bloco:,}")
    except Exception as exc:
        print(f"[2] Bloco: ERRO — {exc}")
        sys.exit(1)

    assets = bot._load_assets()
    print(f"[3] Assets Comet USDC: {len(assets)}")
    for a in assets:
        print(f"    {a['asset'][:20]}… price=${a['price_usd']:,.2f}  lcF={a['liquidate_cf']:.0%}  discount={a['discount']*100:.1f}%")

    orig = bot.scan_blocks
    bot.scan_blocks = 5000
    print("\n[4] A procurar borrowers nos últimos 5000 blocos…")
    bot._scan_borrowers()
    print(f"    Borrowers encontrados: {len(bot._borrowers)}")
    bot.scan_blocks = orig

    print(f"\n[5] A verificar posições (threshold HF={bot.hf_threshold})…")
    found = 0
    for addr in list(bot._borrowers)[:20]:
        opp = bot._check_position(addr)
        if opp is None:
            continue
        status = "⚠️  LIQUIDÁVEL" if opp.is_liquidatable else f"HF={opp.health_factor:.4f}"
        print(f"    {addr[:16]}…  dívida=${opp.total_debt_usd:.2f}  col=${opp.total_collateral_usd:.2f}  {status}")
        if opp.is_liquidatable:
            print(f"       lucro≈${opp.estimated_profit_usd:.4f}  gas≈${opp.gas_cost_usd:.6f}")
            found += 1

    print(f"\n    Total liquidáveis: {found} (dry_run={bot.dry_run})")
    print("\nSMOKE OK")
