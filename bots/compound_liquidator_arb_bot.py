"""Bot de liquidações Compound V3 (Comet USDC + USDT) na chain Arbitrum.

Lógica idêntica ao compound_liquidator_polygon_bot.py, adaptada para Arbitrum:
  - Dois contratos Comet (USDC e USDT) varridos no mesmo tick
  - chain='compound_arb', chain_id=42161
  - RPC: ALCHEMY_ARB_URL do .env (fallback: https://arb1.arbitrum.io/rpc)
  - Gas estimado em ETH (token nativo do Arbitrum)
  - min_profit_usd=25.0  (gas Arbitrum mais caro que Polygon)

Contratos Compound V3 Arbitrum (chain 42161):
  Comet USDC: 0xA5EDBDD9646f8dFF606d7448e414884C7d905dCA
  Comet USDT: 0xd98Be00b5D27fc98112BdE293e487f8D4cA57d07
  Colaterais típicos: WETH, WBTC, ARB, GMX, wstETH

RPC: ALCHEMY_ARB_URL do .env (fallback: https://arb1.arbitrum.io/rpc)
Logs: /opt/crypto_bsc/logs/compound_arb.log + journal (get_logger)
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field

import requests
from web3 import Web3
from web3.exceptions import ContractLogicError

from utils.config import get_env, get_settings
from utils.database import init_db, upsert_liquidation_opportunity
from utils.logger import get_logger
from utils.notifier import TelegramNotifier

logger = get_logger(__name__)

# File handler adicional — registo persistente em ficheiro
_LOG_DIR = "/opt/crypto_bsc/logs"
os.makedirs(_LOG_DIR, exist_ok=True)
_fh = logging.FileHandler(os.path.join(_LOG_DIR, "compound_arb.log"))
_fh.setFormatter(logging.Formatter(
    "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
))
logger.addHandler(_fh)

# ── Compound V3 Arbitrum — constantes ─────────────────────────────────────────

ARB_CHAIN_ID        = 42161
_PRICE_DECIMALS     = 8         # getPrice retorna USD × 1e8
_CF_SCALE           = 1e18      # collateralFactor em 1e18
_GAS_UNITS          = 500_000   # absorb() estimativa conservadora
_ARB_FALLBACK_RPC   = "https://arb.drpc.org"

_HF_LIQUIDATABLE = 1.0
_BLACKLIST_FAILS  = 3

# WETH Arbitrum — usado para estimar preço do gas em USD
_WETH_ADDRESS       = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"
_ETH_PRICE_FALLBACK = 2000.0    # USD, fallback se não encontrar no price feed

# Ambos os Comets Arbitrum
_COMET_CONFIGS = [
    {
        "key":           "usdc",
        "address":       "0xA5EDBDD9646f8dFF606d7448e414884C7d905dCA",
        "base_token":    "USDC",
        "base_decimals": 6,
    },
    {
        "key":           "usdt",
        "address":       "0xd98Be00b5D27fc98112BdE293e487f8D4cA57d07",
        "base_token":    "USDT",
        "base_decimals": 6,
    },
]

# ── ABI mínimo do Comet (idêntico ao Base/Polygon) ────────────────────────────

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
                    {"name": "offset",                   "type": "uint8"},
                    {"name": "asset",                    "type": "address"},
                    {"name": "priceFeed",                "type": "address"},
                    {"name": "scale",                    "type": "uint64"},
                    {"name": "borrowCollateralFactor",   "type": "uint64"},
                    {"name": "liquidateCollateralFactor","type": "uint64"},
                    {"name": "liquidationFactor",        "type": "uint64"},
                    {"name": "supplyCap",                "type": "uint128"},
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
            {"name": "absorber", "type": "address"},
            {"name": "accounts", "type": "address[]"},
        ],
        "name": "absorb",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    # ── eventos ───────────────────────────────────────────────────────────────
    {
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
        "anonymous": False,
        "inputs": [
            {"indexed": True,  "name": "absorber",    "type": "address"},
            {"indexed": True,  "name": "borrower",    "type": "address"},
            {"indexed": False, "name": "basePaidOut", "type": "uint256"},
            {"indexed": False, "name": "usdValue",    "type": "uint256"},
        ],
        "name": "AbsorbDebt",
        "type": "event",
    },
]


@dataclass
class LiqOpportunityCompoundArb:
    comet_key: str
    borrower: str
    health_factor: float
    is_liquidatable: bool
    total_collateral_usd: float
    total_debt_usd: float
    estimated_profit_usd: float
    gas_cost_usd: float
    liquidation_bonus_pct: float
    base_token: str = field(default="USDC")
    best_collateral_asset: str = field(default="")


# ── Bot ───────────────────────────────────────────────────────────────────────

class CompoundLiquidatorArbBot:
    """Monitoriza e (em modo live) executa liquidações Compound V3 USDC+USDT no Arbitrum."""

    def __init__(self, settings: dict | None = None):
        self.settings = settings or get_settings()
        self.cfg = self.settings.get("bots", {}).get("compound_liquidator_arb", {})

        primary_rpc = get_env("ALCHEMY_ARB_URL") or _ARB_FALLBACK_RPC
        self._rpc_urls: list[str] = [primary_rpc, _ARB_FALLBACK_RPC]
        self._active_rpc: str = primary_rpc

        self.dry_run      : bool  = False
        self.hf_threshold : float = float(self.cfg.get("health_factor_threshold", 1.2))
        self.min_profit   : float = float(self.cfg.get("min_profit_usd", 8.0))
        self.scan_blocks  : int   = int(self.cfg.get("borrower_scan_blocks", 200_000))
        self.max_per_tick : int   = int(self.cfg.get("max_positions_per_tick", 50))

        self.w3 = Web3(Web3.HTTPProvider(primary_rpc, request_kwargs={"timeout": 30}))

        # Um contrato instanciado por Comet
        self._contracts: dict[str, object] = {}
        for cfg in _COMET_CONFIGS:
            addr = Web3.to_checksum_address(cfg["address"])
            self._contracts[cfg["key"]] = self.w3.eth.contract(address=addr, abi=_COMET_ABI)

        # Estado separado por Comet
        self._borrowers : dict[str, set[str]]   = {c["key"]: set() for c in _COMET_CONFIGS}
        self._scan_from : dict[str, int]         = {c["key"]: 0     for c in _COMET_CONFIGS}
        self._assets    : dict[str, list[dict]]  = {c["key"]: []    for c in _COMET_CONFIGS}
        self._assets_ts : dict[str, float]       = {c["key"]: 0.0   for c in _COMET_CONFIGS}

        self._eth_price_cache: float = _ETH_PRICE_FALLBACK
        self._eth_price_ts   : float = 0.0
        self._cooldown       : dict[str, float] = {}
        self._fail_counts    : dict[str, int]   = {}
        self._blacklist      : dict[str, float] = {}

        self.notifier = TelegramNotifier(self.settings)
        init_db()

        logger.info(
            "CompoundArb: rpc=%s dry_run=%s hf<%.2f min_profit=$%.2f comets=%s",
            primary_rpc.split("//")[-1].split("/")[0], self.dry_run,
            self.hf_threshold, self.min_profit,
            [c["key"] for c in _COMET_CONFIGS],
        )

    # ── helpers ───────────────────────────────────────────────────────────────

    def _switch_rpc(self, failed_url: str) -> bool:
        for url in self._rpc_urls:
            if url == failed_url:
                continue
            try:
                test_w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 15}))
                test_w3.eth.block_number
                self.w3 = test_w3
                self._active_rpc = url
                # Reconectar todos os contratos ao novo RPC
                for cfg in _COMET_CONFIGS:
                    addr = Web3.to_checksum_address(cfg["address"])
                    self._contracts[cfg["key"]] = self.w3.eth.contract(address=addr, abi=_COMET_ABI)
                logger.warning("CompoundArb: RPC trocado para fallback: %s",
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
                        logger.warning("CompoundArb: rate-limit (429) — a tentar fallback RPC")
                        if self._switch_rpc(self._active_rpc):
                            continue
                    time.sleep(2 ** attempt)
                else:
                    logger.debug("CompoundArb: HTTP %s ao verificar ligação: %s", status, exc)
                    if attempt == 0 and self._active_rpc != _ARB_FALLBACK_RPC:
                        if self._switch_rpc(self._active_rpc):
                            continue
                    return False
            except Exception as exc:
                logger.debug("CompoundArb: erro ao verificar ligação RPC: %s", exc)
                if attempt == 0 and self._active_rpc != _ARB_FALLBACK_RPC:
                    if self._switch_rpc(self._active_rpc):
                        continue
                return False
        return False

    def _eth_price(self) -> float:
        """Preço ETH em USD — tenta via price feed do WETH no primeiro Comet disponível."""
        now = time.time()
        if now - self._eth_price_ts < 300:
            return self._eth_price_cache
        for comet_key in self._contracts:
            assets = self._load_assets(comet_key)
            for a in assets:
                if a["asset"].lower() == _WETH_ADDRESS.lower():
                    self._eth_price_cache = a["price_usd"]
                    self._eth_price_ts    = now
                    return self._eth_price_cache
        return self._eth_price_cache

    def _gas_price_gwei(self) -> float:
        try:
            return self.w3.eth.gas_price / 1e9
        except Exception:
            return 0.1  # Arbitrum: ~0.01–0.5 Gwei típico

    def _load_assets(self, comet_key: str) -> list[dict]:
        """Cache de asset info com preços para um Comet específico (TTL: 5 min)."""
        now = time.time()
        if self._assets[comet_key] and now - self._assets_ts[comet_key] < 300:
            return self._assets[comet_key]
        contract = self._contracts[comet_key]
        try:
            n = contract.functions.numAssets().call()
            assets = []
            for i in range(n):
                info      = contract.functions.getAssetInfo(i).call()
                price_raw = contract.functions.getPrice(info[2]).call()
                assets.append({
                    "asset":         info[1],
                    "price_feed":    info[2],
                    "scale":         info[3],
                    "borrow_cf":     info[4] / _CF_SCALE,
                    "liquidate_cf":  info[5] / _CF_SCALE,
                    "liquidation_f": info[6] / _CF_SCALE,
                    "price_usd":     price_raw / 10 ** _PRICE_DECIMALS,
                    "discount":      1.0 - info[6] / _CF_SCALE,
                })
            self._assets[comet_key]    = assets
            self._assets_ts[comet_key] = now
            logger.debug("CompoundArb[%s]: %d assets carregados", comet_key, len(assets))
        except Exception as exc:
            logger.warning("CompoundArb[%s]: falha ao carregar assets: %s", comet_key, exc)
        return self._assets[comet_key]

    # ── descoberta de borrowers ───────────────────────────────────────────────

    def _scan_borrowers(self, comet_key: str) -> None:
        contract = self._contracts[comet_key]
        try:
            latest = self.w3.eth.block_number
        except Exception as exc:
            logger.warning("CompoundArb[%s]: não consegui ler bloco actual: %s",
                           comet_key, exc)
            return

        from_block = self._scan_from[comet_key] or max(0, latest - self.scan_blocks)
        scan_start = from_block
        chunk, added = 2000, 0

        while from_block < latest:
            to_block = min(from_block + chunk - 1, latest)
            try:
                events = contract.events.Withdraw().get_logs(
                    from_block=from_block, to_block=to_block)
                for e in events:
                    addr = e.args["src"].lower()
                    if addr not in self._borrowers[comet_key]:
                        self._borrowers[comet_key].add(addr)
                        added += 1
                from_block = to_block + 1
            except Exception as exc:
                logger.debug("CompoundArb[%s]: Withdraw events [%d..%d]: %s",
                             comet_key, from_block, to_block, exc)
                break

        self._scan_from[comet_key] = latest

        # Remover posições já liquidadas
        try:
            absorb_start = max(0, latest - 5_000)
            absorb_evts  = contract.events.AbsorbDebt().get_logs(
                from_block=absorb_start, to_block=latest)
            for e in absorb_evts:
                self._borrowers[comet_key].discard(e.args["borrower"].lower())
        except Exception:
            pass

        logger.info(
            "CompoundArb[%s]: scan blocos %d..%d +%d novos borrowers (total=%d)",
            comet_key, scan_start, latest, added, len(self._borrowers[comet_key]),
        )

    # ── análise de posições ───────────────────────────────────────────────────

    def _check_position(self, comet_key: str,
                        address: str) -> LiqOpportunityCompoundArb | None:
        comet_cfg = next(c for c in _COMET_CONFIGS if c["key"] == comet_key)
        contract  = self._contracts[comet_key]
        cs_addr   = Web3.to_checksum_address(address)

        try:
            # 1. Dívida
            debt_raw = contract.functions.borrowBalanceOf(cs_addr).call()
            if debt_raw == 0:
                self._borrowers[comet_key].discard(address)
                return None
            debt_usd = debt_raw / 10 ** comet_cfg["base_decimals"]

            # 2. isLiquidatable
            is_liq = contract.functions.isLiquidatable(cs_addr).call()

            # 3. Colateral
            assets = self._load_assets(comet_key)
            if not assets:
                return None

            collateral_usd    = 0.0
            liq_threshold_usd = 0.0
            best_asset        = ""
            best_col_usd      = 0.0

            for a in assets:
                try:
                    bal = contract.functions.collateralBalanceOf(
                        cs_addr, Web3.to_checksum_address(a["asset"])).call()
                    if bal == 0:
                        continue
                    val_usd            = (bal / a["scale"]) * a["price_usd"]
                    collateral_usd    += val_usd
                    liq_threshold_usd += val_usd * a["liquidate_cf"]
                    if val_usd > best_col_usd:
                        best_col_usd = val_usd
                        best_asset   = a["asset"]
                except Exception:
                    pass

            # 4. Health factor estilo Aave (liq_threshold / debt)
            if liq_threshold_usd > 0 and debt_usd > 0:
                pseudo_hf = liq_threshold_usd / debt_usd
            elif is_liq:
                pseudo_hf = 0.0
            else:
                self._borrowers[comet_key].discard(address)
                return None

            # 5. Filtrar posições saudáveis
            if not is_liq and pseudo_hf >= self.hf_threshold:
                return None

            # 6. Gas em ETH
            gas_usd = _GAS_UNITS * self._gas_price_gwei() * 1e-9 * self._eth_price()

            # 7. Lucro estimado — usa pseudo_hf < 1.0 como fallback se isLiquidatable() divergir
            effectively_liq = is_liq or pseudo_hf < 1.0
            if effectively_liq:
                net_profit = max(debt_usd * 0.08 - gas_usd, 0.0)
                bonus_pct  = 8.0
            else:
                net_profit = 0.0
                bonus_pct  = 0.0

            return LiqOpportunityCompoundArb(
                comet_key=comet_key,
                borrower=address,
                health_factor=round(pseudo_hf, 6),
                is_liquidatable=effectively_liq,
                total_collateral_usd=round(collateral_usd, 2),
                total_debt_usd=round(debt_usd, 2),
                estimated_profit_usd=round(net_profit, 4),
                gas_cost_usd=round(gas_usd, 6),
                liquidation_bonus_pct=bonus_pct,
                base_token=comet_cfg["base_token"],
                best_collateral_asset=best_asset,
            )

        except (ContractLogicError, Exception) as exc:
            logger.debug("CompoundArb[%s]: posição %s…: %s",
                         comet_key, address[:10], exc)
            return None

    # ── execução / registo ────────────────────────────────────────────────────

    def _record(self, opp: LiqOpportunityCompoundArb) -> None:
        status = "dry_run" if self.dry_run else (
            "liquidatable" if opp.is_liquidatable else "watching"
        )
        position_key = f"{opp.comet_key}:{opp.borrower}"
        rec_id, inserted = upsert_liquidation_opportunity(
            position_address=position_key,
            health_factor=opp.health_factor,
            debt_asset=opp.base_token,
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
            chain="compound_arb",
        )
        action = "INSERT" if inserted else "UPDATE"
        logger.debug(
            "CompoundArb[%s]: BD %s id=%d %s HF=%.4f liq=%s",
            opp.comet_key, action, rec_id, opp.borrower[:10],
            opp.health_factor, opp.is_liquidatable,
        )

    def _execute_live(self, opp: LiqOpportunityCompoundArb, nonce: int) -> str | None:
        _cd_key = f"{opp.comet_key}:{opp.borrower.lower()}"
        try:
            pk       = get_env("BSC_PRIVATE_KEY") or ""
            acct     = self.w3.eth.account.from_key(pk)
            contract = self._contracts[opp.comet_key]
            try:
                contract.functions.absorb(
                    acct.address,
                    [Web3.to_checksum_address(opp.borrower)],
                ).call({"from": acct.address})
            except Exception as sim_exc:
                self._cooldown[_cd_key] = time.time() + 120
                logger.warning(
                    "CompoundArb[%s]: simulação falhou %s — cooldown 2min: %s",
                    opp.comet_key, opp.borrower[:10] + "…", sim_exc,
                )
                return None
            tx = contract.functions.absorb(
                acct.address,
                [Web3.to_checksum_address(opp.borrower)],
            ).build_transaction({
                "from":     acct.address,
                "chainId":  ARB_CHAIN_ID,
                "gas":      _GAS_UNITS,
                "gasPrice": int(self.w3.eth.gas_price * 1.15),
                "nonce":    nonce,
            })
            signed  = acct.sign_transaction(tx)
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            logger.info("CompoundArb[%s]: ABSORB TX: %s", opp.comet_key, tx_hash.hex())
            return tx_hash.hex()
        except Exception as exc:
            self._cooldown[_cd_key] = time.time() + 300
            logger.warning("CompoundArb[%s]: absorb revertida — cooldown 5min: %s",
                           opp.comet_key, exc)
            return None

    # ── tick ──────────────────────────────────────────────────────────────────

    def tick(self) -> list[dict]:
        if not self._connected():
            logger.warning("CompoundArb: sem ligação ao RPC Arbitrum — tick saltado")
            return []

        _pk    = get_env("BSC_PRIVATE_KEY") or ""
        _acct  = self.w3.eth.account.from_key(_pk)
        _nonce = self.w3.eth.get_transaction_count(_acct.address, 'pending')
        if not self.dry_run:
            _bal_wei = self.w3.eth.get_balance(_acct.address)
            if _bal_wei < Web3.to_wei(0.005, 'ether'):
                logger.error(
                    "CompoundArb: saldo insuficiente (%.6f ETH < 0.005) — execução suspensa",
                    float(Web3.from_wei(_bal_wei, 'ether')),
                )
                return []

        results: list[dict] = []

        for comet_cfg in _COMET_CONFIGS:
            comet_key = comet_cfg["key"]

            self._scan_borrowers(comet_key)
            self._load_assets(comet_key)

            _now_tick = time.time()
            logger.info("CompoundArb[%s] Tick: %d borrowers | %d blacklist",
                        comet_key, len(self._borrowers[comet_key]), len(self._blacklist))

            liq_count = watch_count = 0

            for borrower in list(self._borrowers[comet_key])[:self.max_per_tick]:
                _cd_key = f"{comet_key}:{borrower.lower()}"

                opp = self._check_position(comet_key, borrower)
                if opp is None:
                    continue

                if _cd_key in self._blacklist:
                    _bl_hf = self._blacklist[_cd_key]
                    if abs(opp.health_factor - _bl_hf) / max(_bl_hf, 0.001) < 0.05:
                        logger.debug("CompoundArb[%s]: %s blacklisted — saltado",
                                     comet_key, borrower[:10] + "…")
                        continue
                    del self._blacklist[_cd_key]
                    self._fail_counts.pop(_cd_key, None)
                    logger.info("CompoundArb[%s]: %s saiu da blacklist",
                                comet_key, borrower[:10] + "…")

                tx_hash = None
                if opp.is_liquidatable:
                    liq_count += 1
                    logger.info(
                        "CompoundArb[%s]: LIQUIDÁVEL %s HF=%.4f dívida=$%.2f "
                        "col=$%.2f lucro≈$%.4f dry=%s",
                        comet_key, borrower[:10] + "…", opp.health_factor,
                        opp.total_debt_usd, opp.total_collateral_usd,
                        opp.estimated_profit_usd, self.dry_run,
                    )
                    if not self.dry_run and opp.estimated_profit_usd >= self.min_profit:
                        _until = self._cooldown.get(_cd_key, 0)
                        if _until > _now_tick:
                            logger.debug("CompoundArb[%s]: %s em cooldown (%.0fs)",
                                         comet_key, borrower[:10] + "…", _until - _now_tick)
                        else:
                            tx_hash = self._execute_live(opp, nonce=_nonce)
                            if tx_hash is None:
                                _cnt = self._fail_counts.get(_cd_key, 0) + 1
                                self._fail_counts[_cd_key] = _cnt
                                if _cnt >= _BLACKLIST_FAILS:
                                    self._blacklist[_cd_key] = opp.health_factor
                                    logger.warning(
                                        "CompoundArb[%s]: %s adicionado à blacklist (%d falhas)",
                                        comet_key, borrower[:10] + "…", _cnt,
                                    )
                            else:
                                self._fail_counts.pop(_cd_key, None)
                                _nonce += 1
                                self.notifier.notify(
                                    "trade_executed",
                                    f"🟢 Compound Arb[{comet_key}] ABSORB "
                                    f"{borrower[:10]}… "
                                    f"lucro≈${opp.estimated_profit_usd:.2f} "
                                    f"| tx={tx_hash[:20]}…",
                                )
                else:
                    watch_count += 1
                    logger.debug(
                        "CompoundArb[%s]: vigiar %s HF=%.4f dívida=$%.2f",
                        comet_key, borrower[:10] + "…", opp.health_factor, opp.total_debt_usd,
                    )

                self._record(opp)
                results.append({
                    "comet":           comet_key,
                    "borrower":        borrower,
                    "hf":              opp.health_factor,
                    "is_liquidatable": opp.is_liquidatable,
                    "debt_usd":        opp.total_debt_usd,
                    "profit_usd":      opp.estimated_profit_usd,
                    "executed":        tx_hash is not None,
                    "dry_run":         self.dry_run,
                })

            logger.info(
                "CompoundArb[%s]: tick — %d liquidáveis / %d em vigilância (%d borrowers)",
                comet_key, liq_count, watch_count, len(self._borrowers[comet_key]),
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
    print("COMPOUND V3 ARBITRUM LIQUIDATOR — Smoke Test")
    print("=" * 60)

    bot = CompoundLiquidatorArbBot()

    connected = bot._connected()
    print(f"\n[1] RPC Arbitrum conectado: {connected}")
    if not connected:
        sys.exit(0)

    try:
        bloco = bot.w3.eth.block_number
        print(f"[2] Bloco actual Arbitrum: {bloco:,}")
    except Exception as exc:
        print(f"[2] Bloco: ERRO — {exc}")
        sys.exit(1)

    for cfg in _COMET_CONFIGS:
        key = cfg["key"]
        assets = bot._load_assets(key)
        print(f"\n[3] Assets Comet {key.upper()}: {len(assets)}")
        for a in assets:
            print(f"    {a['asset'][:22]}…  price=${a['price_usd']:,.4f}"
                  f"  lcF={a['liquidate_cf']:.0%}  discount={a['discount']*100:.1f}%")

    print(f"\n[4] Preço ETH:  ${bot._eth_price():.2f}")
    print(f"    Gas price:  {bot._gas_price_gwei():.4f} Gwei")
    gas_usd = _GAS_UNITS * bot._gas_price_gwei() * 1e-9 * bot._eth_price()
    print(f"    Gas custo:  ${gas_usd:.4f} USD")

    for cfg in _COMET_CONFIGS:
        key = cfg["key"]
        orig = bot.scan_blocks
        bot.scan_blocks = 5000
        print(f"\n[5] A procurar borrowers {key.upper()} (últimos 5000 blocos)…")
        bot._scan_borrowers(key)
        print(f"    Borrowers encontrados: {len(bot._borrowers[key])}")
        bot.scan_blocks = orig

        print(f"\n[6] A verificar posições {key.upper()} (threshold HF={bot.hf_threshold})…")
        found = 0
        for addr in list(bot._borrowers[key])[:20]:
            opp = bot._check_position(key, addr)
            if opp is None:
                continue
            status = "⚠️  LIQUIDÁVEL" if opp.is_liquidatable else f"HF={opp.health_factor:.4f}"
            print(f"    {addr[:16]}…  dívida=${opp.total_debt_usd:.2f}  "
                  f"col=${opp.total_collateral_usd:.2f}  {status}")
            if opp.is_liquidatable:
                print(f"       lucro≈${opp.estimated_profit_usd:.4f}  "
                      f"gas≈${opp.gas_cost_usd:.6f}")
                found += 1
        print(f"    Total liquidáveis {key.upper()}: {found} (dry_run={bot.dry_run})")

    print("\nSMOKE OK")
