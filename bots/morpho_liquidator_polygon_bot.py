"""Bot de liquidações Morpho Blue na chain Polygon.

Morpho Blue usa mercados isolados por (loanToken, collateralToken, oracle, irm, lltv).
Cada mercado tem o seu próprio LLTV e LIF (Liquidation Incentive Factor).

Diferenças vs Aave/Compound:
  - Múltiplos mercados independentes (não um pool global)
  - Sem flash loan necessário — liquidação directa com repayment
  - LIF = min(1.15, 1/(0.3×LLTV + 0.7)) — varia por mercado
  - Health factor: (collateral × oraclePrice × LLTV) / (borrow × 1e36 × 1e18)
  - Oracle já incorpora scaling de decimais → collateral_value = col × price / 1e36

Fluxo por tick:
  1. Scan eventos Borrow → (market_id, borrower) tracking
  2. Para cada posição: position() + market() + oracle.price()
  3. Se HF < 1: liquidável; se 1 < HF < threshold: vigiar
  4. Lucro estimado = borrow_usd × (LIF - 1) - gas

Contrato Morpho Blue Polygon (chain 137):
  0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import threading
import time
import websockets
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import requests
from web3 import Web3
from web3.exceptions import ContractLogicError

from utils.config import get_env, get_settings
from utils.database import init_db, upsert_liquidation_opportunity
from utils.flashbots import send_bundle as _fb_send_bundle
from utils.flashbots import send_bundle_multi as _fb_send_multi
from utils.logger import get_logger
from utils.notifier import TelegramNotifier
from utils.wallet_pool import WalletPool

logger = get_logger(__name__)

# File handler adicional — registo persistente em ficheiro
_LOG_DIR = "/opt/crypto_bsc/logs"
os.makedirs(_LOG_DIR, exist_ok=True)
_fh = logging.FileHandler(os.path.join(_LOG_DIR, "morpho_polygon.log"))
_fh.setFormatter(logging.Formatter(
    "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
))
logger.addHandler(_fh)

# ── Morpho Blue Polygon ────────────────────────────────────────────────────────

MORPHO_ADDRESS    = Web3.to_checksum_address("0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb")
POLYGON_CHAIN_ID  = 137
_ORACLE_SCALE     = 10 ** 36   # Morpho oracles: price × 1e36
_LLTV_SCALE       = 10 ** 18   # LLTV em WAD
_GAS_UNITS        = 350_000    # liquidate() na Polygon
_FALLBACK_RPC_1   = "https://polygon.drpc.org"
_FALLBACK_RPC_2       = "https://rpc.ankr.com/polygon"
_POLYGON_WSS_PRIMARY  = "wss://ws-nd-885-494-770.p2pify.com/c3d7b889db6c1eb7cfc287501eea2d19"
_POLYGON_WSS_FALLBACK = "wss://polygon.drpc.org"

_SCAN_INTERVAL_BLOCKS = 60   # ~2 min em Polygon (2s/bloco)

_HF_LIQUIDATABLE = 1.0

_FLASHBOTS_ENDPOINT      = "https://polygon.flashbots.net"
_FLASHBOTS_MIN_PROFIT_USD = 200.0
_BLACKLIST_FAILS  = 3
_MAX_BUNDLE_TXS   = 4

# Stablecoins Polygon (minúsculas): (symbol, decimals, usd_price)
_STABLE_TOKENS: dict[str, tuple[str, int, float]] = {
    "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359": ("USDC",   6, 1.0),  # USDC native
    "0xc2132d05d31c914a87c6611c10748aeb04b58e8f": ("USDT",   6, 1.0),  # USDT
    "0x8f3cf7ad23cd3cadbd9735aff958023239c6a063": ("DAI",   18, 1.0),  # DAI
    "0x2791bca1f2de4661ed88a30c99a7a9449aa84174": ("USDC.e", 6, 1.0),  # USDC bridged
}
_WMATIC_ADDRESS = "0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270"

# ── ABI ───────────────────────────────────────────────────────────────────────

_MORPHO_ABI = [
    # ── view ──────────────────────────────────────────────────────────────────
    {
        "inputs": [{"name": "id", "type": "bytes32"}],
        "name": "idToMarketParams",
        "outputs": [{"type": "tuple", "components": [
            {"name": "loanToken",       "type": "address"},
            {"name": "collateralToken", "type": "address"},
            {"name": "oracle",          "type": "address"},
            {"name": "irm",             "type": "address"},
            {"name": "lltv",            "type": "uint256"},
        ]}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "id", "type": "bytes32"}],
        "name": "market",
        "outputs": [{"type": "tuple", "components": [
            {"name": "totalSupplyAssets", "type": "uint128"},
            {"name": "totalSupplyShares", "type": "uint128"},
            {"name": "totalBorrowAssets", "type": "uint128"},
            {"name": "totalBorrowShares", "type": "uint128"},
            {"name": "lastUpdate",        "type": "uint128"},
            {"name": "fee",               "type": "uint128"},
        ]}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "id",   "type": "bytes32"},
            {"name": "user", "type": "address"},
        ],
        "name": "position",
        "outputs": [{"type": "tuple", "components": [
            {"name": "supplyShares", "type": "uint256"},
            {"name": "borrowShares", "type": "uint128"},
            {"name": "collateral",   "type": "uint128"},
        ]}],
        "stateMutability": "view",
        "type": "function",
    },
    # ── write (só usado em live) ──────────────────────────────────────────────
    {
        "inputs": [
            {"name": "marketParams", "type": "tuple", "components": [
                {"name": "loanToken",       "type": "address"},
                {"name": "collateralToken", "type": "address"},
                {"name": "oracle",          "type": "address"},
                {"name": "irm",             "type": "address"},
                {"name": "lltv",            "type": "uint256"},
            ]},
            {"name": "borrower",     "type": "address"},
            {"name": "seizedAssets", "type": "uint256"},
            {"name": "repaidShares", "type": "uint256"},
            {"name": "data",         "type": "bytes"},
        ],
        "name": "liquidate",
        "outputs": [
            {"name": "seizedAssets", "type": "uint256"},
            {"name": "repaidAssets", "type": "uint256"},
        ],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    # ── eventos ───────────────────────────────────────────────────────────────
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True,  "name": "id",       "type": "bytes32"},
            {"indexed": False, "name": "caller",   "type": "address"},
            {"indexed": True,  "name": "onBehalf", "type": "address"},
            {"indexed": True,  "name": "receiver", "type": "address"},
            {"indexed": False, "name": "assets",   "type": "uint256"},
            {"indexed": False, "name": "shares",   "type": "uint256"},
        ],
        "name": "Borrow",
        "type": "event",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True,  "name": "id",             "type": "bytes32"},
            {"indexed": True,  "name": "caller",         "type": "address"},
            {"indexed": True,  "name": "borrower",       "type": "address"},
            {"indexed": False, "name": "repaidAssets",   "type": "uint256"},
            {"indexed": False, "name": "repaidShares",   "type": "uint256"},
            {"indexed": False, "name": "seizedAssets",   "type": "uint256"},
            {"indexed": False, "name": "badDebtAssets",  "type": "uint256"},
            {"indexed": False, "name": "badDebtShares",  "type": "uint256"},
        ],
        "name": "Liquidate",
        "type": "event",
    },
]

_ORACLE_ABI = [
    {
        "inputs": [],
        "name": "price",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]

_ERC20_ABI = [
    {"inputs": [], "name": "decimals", "outputs": [{"type": "uint8"}],  "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "symbol",   "outputs": [{"type": "string"}], "stateMutability": "view", "type": "function"},
]


@dataclass
class LiqOpportunityMorphoPolygon:
    position_key: str          # "{borrower}:{market_id[:10]}"
    borrower: str
    market_id: str
    loan_symbol: str
    collateral_symbol: str
    health_factor: float
    borrow_usd: float
    collateral_usd: float
    estimated_profit_usd: float
    gas_cost_usd: float
    lif_pct: float             # (LIF - 1) × 100
    lltv_pct: float


# ── Bot ───────────────────────────────────────────────────────────────────────

class MorphoLiquidatorPolygonBot:
    """Monitoriza e (em modo live) executa liquidações Morpho Blue na Polygon."""

    def __init__(self, settings: dict | None = None):
        self.settings = settings or get_settings()
        self.cfg = self.settings.get("bots", {}).get("morpho_liquidator_polygon", {})

        primary_rpc = get_env("ALCHEMY_POLYGON_URL") or _FALLBACK_RPC_1
        self._rpc_urls: list[str] = [primary_rpc, _FALLBACK_RPC_1, _FALLBACK_RPC_2]
        self._active_rpc: str = primary_rpc

        self.dry_run      : bool  = False
        self.hf_threshold : float = float(self.cfg.get("health_factor_threshold", 1.2))
        self.min_profit   : float = float(self.cfg.get("min_profit_usd", 5.0))
        self.scan_blocks  : int   = int(self.cfg.get("borrower_scan_blocks", 200_000))
        self.max_per_tick : int   = int(self.cfg.get("max_positions_per_tick", 100))

        self.w3     = Web3(Web3.HTTPProvider(primary_rpc, request_kwargs={"timeout": 30}))
        self.morpho = self.w3.eth.contract(address=MORPHO_ADDRESS, abi=_MORPHO_ABI)

        # market_id_hex → {loanToken, collateralToken, oracle, irm, lltv}
        self._markets: dict[str, dict] = {}
        # (market_id_hex, borrower_lower) → True
        self._positions: dict[tuple[str, str], bool] = {}
        self._scan_from: int = 0

        # Caches
        self._token_cache  : dict[str, tuple[str, int, float]] = {}
        self._oracle_cache : dict[str, tuple[int, float]] = {}  # addr → (price_raw, ts)
        self._matic_price_cache: float = 0.80   # USD fallback
        self._matic_price_ts   : float = 0.0
        self._cooldown         : dict[str, float] = {}
        self._fail_counts      : dict[str, int]   = {}
        self._blacklist        : dict[str, float] = {}

        self._block_queue:  queue.Queue = queue.Queue(maxsize=20)
        self._last_block:   int   = 0
        self._ws_last_seen: float = time.time()
        self._ws_stop       = threading.Event()
        self._ws_thread     = threading.Thread(
            target=self._ws_runner, daemon=True, name="morpho-polygon-ws")
        self._ws_thread.start()

        self.notifier = TelegramNotifier(self.settings)
        self._wallet_pool = WalletPool()
        init_db()

        logger.info(
            "MorphoPolygon: rpc=%s dry_run=%s hf<%.2f min_profit=$%.2f",
            primary_rpc.split("//")[-1].split("/")[0], self.dry_run,
            self.hf_threshold, self.min_profit,
        )

    # ── helpers ───────────────────────────────────────────────────────────────

    def _switch_rpc(self, failed_url: str) -> bool:
        for url in self._rpc_urls:
            if url == failed_url:
                continue
            try:
                test_w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 15}))
                test_w3.eth.block_number
                self.w3     = test_w3
                self._active_rpc = url
                self.morpho = self.w3.eth.contract(address=MORPHO_ADDRESS, abi=_MORPHO_ABI)
                self._oracle_cache.clear()
                logger.warning("MorphoPolygon: RPC → %s", url.split("//")[-1].split("/")[0])
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
                if status == 429 and attempt == 0:
                    logger.warning("MorphoPolygon: rate-limit (429) — fallback RPC")
                    if self._switch_rpc(self._active_rpc):
                        continue
                time.sleep(2 ** attempt)
            except Exception as exc:
                logger.debug("MorphoPolygon: erro RPC: %s", exc)
                if attempt == 0 and self._active_rpc != _FALLBACK_RPC_1:
                    if self._switch_rpc(self._active_rpc):
                        continue
                return False
        return False

    def _gas_price_gwei(self) -> float:
        try:
            return self.w3.eth.gas_price / 1e9
        except Exception:
            return 30.0  # Polygon: ~30–100 Gwei típico

    def _matic_price(self) -> float:
        now = time.time()
        if now - self._matic_price_ts < 300:
            return self._matic_price_cache
        self._matic_price_ts = now
        return self._matic_price_cache

    def _token_info(self, address: str) -> tuple[str, int, float]:
        """(symbol, decimals, usd_price) — stablecoins e WMATIC são conhecidos."""
        addr = address.lower()
        if addr in self._token_cache:
            return self._token_cache[addr]
        if addr in _STABLE_TOKENS:
            info = _STABLE_TOKENS[addr]
            self._token_cache[addr] = info
            return info
        if addr == _WMATIC_ADDRESS:
            info = ("WMATIC", 18, self._matic_price())
            self._token_cache[addr] = info
            return info
        try:
            tok = self.w3.eth.contract(address=Web3.to_checksum_address(address), abi=_ERC20_ABI)
            sym = tok.functions.symbol().call()
            dec = tok.functions.decimals().call()
            info = (sym, dec, 1.0)
        except Exception:
            info = ("???", 18, 1.0)
        self._token_cache[addr] = info
        return info

    def _oracle_price_raw(self, oracle_address: str) -> int | None:
        """Chama price() no oracle. TTL: 2 min."""
        now = time.time()
        cached = self._oracle_cache.get(oracle_address)
        if cached and now - cached[1] < 120:
            return cached[0]
        try:
            oracle = self.w3.eth.contract(
                address=Web3.to_checksum_address(oracle_address), abi=_ORACLE_ABI)
            price = oracle.functions.price().call()
            self._oracle_cache[oracle_address] = (price, now)
            return price
        except Exception as exc:
            logger.debug("MorphoPolygon: oracle %s…: %s", oracle_address[:10], exc)
            return None

    def _market_params(self, mid_hex: str) -> dict | None:
        """Retorna params do mercado do cache ou da chain."""
        if mid_hex in self._markets:
            return self._markets[mid_hex]
        try:
            mp = self.morpho.functions.idToMarketParams(bytes.fromhex(mid_hex)).call()
            params = {
                "loanToken":       mp[0],
                "collateralToken": mp[1],
                "oracle":          mp[2],
                "irm":             mp[3],
                "lltv":            mp[4],
            }
            self._markets[mid_hex] = params
            return params
        except Exception:
            return None

    # ── descoberta de posições ────────────────────────────────────────────────

    def _scan_borrowers(self) -> None:
        try:
            latest = self.w3.eth.block_number
        except Exception as exc:
            logger.warning("MorphoPolygon: bloco actual: %s", exc)
            return

        from_block = self._scan_from or max(0, latest - self.scan_blocks)
        scan_start = from_block
        chunk, added = 2_000, 0

        while from_block < latest:
            to_block = min(from_block + chunk - 1, latest)
            try:
                events = self.morpho.events.Borrow().get_logs(
                    from_block=from_block, to_block=to_block)
                for e in events:
                    mid    = e.args["id"].hex()
                    borrow = e.args["onBehalf"].lower()
                    key    = (mid, borrow)
                    if key not in self._positions:
                        self._positions[key] = True
                        added += 1
                        if mid not in self._markets:
                            self._market_params(mid)
                from_block = to_block + 1
            except Exception as exc:
                logger.debug("MorphoPolygon: Borrow [%d..%d]: %s", from_block, to_block, exc)
                break

        # Remover posições liquidadas recentemente
        try:
            liq_start = max(0, latest - 5_000)
            liq_evts = self.morpho.events.Liquidate().get_logs(
                from_block=liq_start, to_block=latest)
            for e in liq_evts:
                mid    = e.args["id"].hex()
                borrow = e.args["borrower"].lower()
                self._positions.pop((mid, borrow), None)
        except Exception:
            pass

        self._scan_from = latest
        logger.info(
            "MorphoPolygon: scan %d..%d +%d posições (total=%d, %d mercados)",
            scan_start, latest, added, len(self._positions), len(self._markets),
        )

    # ── análise de posição ────────────────────────────────────────────────────

    def _check_position(self, market_id: str,
                        borrower: str) -> LiqOpportunityMorphoPolygon | None:
        market = self._market_params(market_id)
        if not market:
            return None

        mid_bytes = bytes.fromhex(market_id)
        cs_borrow = Web3.to_checksum_address(borrower)

        try:
            # 1. Posição do borrower
            pos           = self.morpho.functions.position(mid_bytes, cs_borrow).call()
            borrow_shares = pos[1]
            collateral    = pos[2]

            if borrow_shares == 0:
                self._positions.pop((market_id, borrower), None)
                return None

            # 2. Estado do mercado (shares → assets)
            mkt                 = self.morpho.functions.market(mid_bytes).call()
            total_borrow_assets = mkt[2]
            total_borrow_shares = mkt[3]

            if total_borrow_shares == 0:
                return None

            borrow_raw = int(borrow_shares) * int(total_borrow_assets) // int(total_borrow_shares)
            if borrow_raw == 0:
                self._positions.pop((market_id, borrower), None)
                return None

            # 3. Oracle price
            oracle_raw = self._oracle_price_raw(market["oracle"])
            if not oracle_raw:
                return None

            # 4. Health factor: (col × price × lltv) / (borrow × 1e36 × 1e18)
            lltv = int(market["lltv"])
            col  = int(collateral)

            if col == 0:
                if not borrow_raw:
                    self._positions.pop((market_id, borrower), None)
                    return None
                hf = 0.0
            else:
                hf = float(col * oracle_raw * lltv) / float(borrow_raw * _ORACLE_SCALE * _LLTV_SCALE)

            # 5. Filtrar posições saudáveis
            if hf >= self.hf_threshold:
                return None

            # 6. Valor em USD
            loan_sym, loan_dec, loan_price = self._token_info(market["loanToken"])
            col_sym,  col_dec,  _          = self._token_info(market["collateralToken"])

            borrow_usd = (borrow_raw / 10 ** loan_dec) * loan_price

            collateral_value_loan_raw = float(col) * float(oracle_raw) / float(_ORACLE_SCALE)
            collateral_usd = (collateral_value_loan_raw / 10 ** loan_dec) * loan_price

            # 7. LIF e lucro estimado (gas em MATIC)
            lltv_ratio = lltv / _LLTV_SCALE
            lif        = min(1.15, 1.0 / (0.3 * lltv_ratio + 0.7))
            lif_bonus  = lif - 1.0
            gas_usd    = _GAS_UNITS * self._gas_price_gwei() * 1e-9 * self._matic_price()

            if hf < 1.0:
                net_profit = max(borrow_usd * lif_bonus - gas_usd, 0.0)
            else:
                net_profit = max(borrow_usd * 0.5 * lif_bonus - gas_usd, 0.0)

            return LiqOpportunityMorphoPolygon(
                position_key=f"{borrower}:{market_id[:10]}",
                borrower=borrower,
                market_id=market_id,
                loan_symbol=loan_sym,
                collateral_symbol=col_sym,
                health_factor=round(hf, 6),
                borrow_usd=round(borrow_usd, 2),
                collateral_usd=round(collateral_usd, 2),
                estimated_profit_usd=round(net_profit, 4),
                gas_cost_usd=round(gas_usd, 6),
                lif_pct=round(lif_bonus * 100, 2),
                lltv_pct=round(lltv_ratio * 100, 1),
            )

        except (ContractLogicError, Exception) as exc:
            logger.debug("MorphoPolygon: %s:%s…: %s", market_id[:8], borrower[:10], exc)
            return None

    # ── registo na BD ─────────────────────────────────────────────────────────

    def _record(self, opp: LiqOpportunityMorphoPolygon) -> None:
        is_liq = opp.health_factor < 1.0
        status = "dry_run" if self.dry_run else ("liquidatable" if is_liq else "watching")
        rec_id, inserted = upsert_liquidation_opportunity(
            position_address=opp.position_key,
            health_factor=opp.health_factor,
            debt_asset=opp.loan_symbol,
            debt_amount_usd=opp.borrow_usd,
            collateral_asset=opp.collateral_symbol,
            collateral_amount_usd=opp.collateral_usd,
            liquidation_bonus_pct=opp.lif_pct,
            estimated_profit_usd=opp.estimated_profit_usd,
            gas_cost_usd=opp.gas_cost_usd,
            executed=False,
            tx_hash=None,
            dry_run=self.dry_run,
            status=status,
            chain="morpho_polygon",
        )
        logger.debug(
            "MorphoPolygon: BD %s id=%d %s/%s HF=%.4f liq=%s profit=$%.2f",
            "INS" if inserted else "UPD", rec_id,
            opp.collateral_symbol, opp.loan_symbol,
            opp.health_factor, is_liq, opp.estimated_profit_usd,
        )

    # ── execução live ─────────────────────────────────────────────────────────

    def _execute_live(self, opp: LiqOpportunityMorphoPolygon, nonce: int | None = None, *, pk: str | None = None) -> str | None:
        _cd_key = f"{opp.market_id[:10]}:{opp.borrower.lower()}"
        market = self._market_params(opp.market_id)
        if not market:
            return None
        try:
            pk   = pk or get_env("BSC_PRIVATE_KEY") or ""
            acct = self.w3.eth.account.from_key(pk)
            if nonce is None:
                nonce = self.w3.eth.get_transaction_count(acct.address, 'pending')
            mp_tuple = (
                Web3.to_checksum_address(market["loanToken"]),
                Web3.to_checksum_address(market["collateralToken"]),
                Web3.to_checksum_address(market["oracle"]),
                Web3.to_checksum_address(market["irm"]),
                market["lltv"],
            )
            try:
                self.morpho.functions.liquidate(
                    mp_tuple,
                    Web3.to_checksum_address(opp.borrower),
                    0, 0, b"",
                ).call({"from": acct.address})
            except Exception as sim_exc:
                self._cooldown[_cd_key] = time.time() + 120
                logger.warning(
                    "MorphoPolygon: simulação falhou %s — cooldown 2min: %s",
                    opp.borrower[:10] + "…", sim_exc,
                )
                return None
            tx = self.morpho.functions.liquidate(
                mp_tuple,
                Web3.to_checksum_address(opp.borrower),
                0, 0, b"",
            ).build_transaction({
                "from":     acct.address,
                "chainId":  POLYGON_CHAIN_ID,
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
                        logger.info("MorphoPolygon: TX via Flashbots @ bloco %d: %s…", _tgt, _exp.hex()[:18])
                        return _exp.hex()
                except Exception as _fb_exc:
                    logger.warning("MorphoPolygon: Flashbots falhou — fallback mempool: %s", _fb_exc)
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            logger.info("MorphoPolygon: LIQUIDATE TX: %s", tx_hash.hex())
            return tx_hash.hex()
        except Exception as exc:
            self._cooldown[_cd_key] = time.time() + 300
            logger.warning("MorphoPolygon: liquidate revertida — cooldown 5min: %s", exc)
            return None

    # ── bundle helpers ────────────────────────────────────────────────────────

    def _sign_tx_nosim(self, opp: LiqOpportunityMorphoPolygon, pk: str, nonce: int) -> bytes | None:
        """Build+sign liquidate tx without eth_call simulation (for bundle use)."""
        market = self._market_params(opp.market_id)
        if not market:
            return None
        try:
            from eth_account import Account as _Acct
            if pk.startswith("0x"):
                pk = pk[2:]
            acct = _Acct.from_key(pk)
            mp_tuple = (
                Web3.to_checksum_address(market["loanToken"]),
                Web3.to_checksum_address(market["collateralToken"]),
                Web3.to_checksum_address(market["oracle"]),
                Web3.to_checksum_address(market["irm"]),
                market["lltv"],
            )
            tx = self.morpho.functions.liquidate(
                mp_tuple,
                Web3.to_checksum_address(opp.borrower),
                0, 0, b"",
            ).build_transaction({
                "from":     acct.address,
                "chainId":  POLYGON_CHAIN_ID,
                "gas":      _GAS_UNITS,
                "gasPrice": int(self.w3.eth.gas_price * 1.15),
                "nonce":    nonce,
            })
            signed = _Acct.sign_transaction(tx, pk)
            return bytes(signed.raw_transaction)
        except Exception as exc:
            logger.warning("MorphoPolygon: _sign_tx_nosim falhou %s: %s",
                           opp.borrower[:10] + "…", exc)
            return None

    def _try_bundle(self, to_exec: list[tuple]) -> set[str]:
        """Sign all txs (no sim) and submit as Flashbots bundle. Returns bundled cd_keys."""
        if len(to_exec) < 2:
            return set()
        try:
            pk = get_env("BSC_PRIVATE_KEY") or ""
            pk = pk.strip().strip('"').strip("'")
            if pk.startswith("0x"):
                pk = pk[2:]
            from eth_account import Account as _Acct
            acct       = _Acct.from_key(pk)
            base_nonce = self.w3.eth.get_transaction_count(acct.address)
            target     = self.w3.eth.block_number + 1
            raw_txes, cd_keys = [], []
            for i, (opp, _cd_key) in enumerate(to_exec[:_MAX_BUNDLE_TXS]):
                raw = self._sign_tx_nosim(opp, pk, base_nonce + i)
                if raw is None:
                    continue
                raw_txes.append("0x" + raw.hex())
                cd_keys.append(_cd_key)
            if len(raw_txes) < 2:
                return set()
            bh = _fb_send_multi(raw_txes, target, _FLASHBOTS_ENDPOINT, pk)
            if bh:
                logger.info("MorphoPolygon: bundle %d txs @ bloco %d: %s…",
                            len(raw_txes), target, bh[:16])
                return set(cd_keys)
            return set()
        except Exception as exc:
            logger.warning("MorphoPolygon: bundle falhou → fallback individual: %s", exc)
            return set()

    # ── tick ──────────────────────────────────────────────────────────────────

    def _ws_runner(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._ws_listen())
        finally:
            loop.close()

    async def _ws_listen(self) -> None:
        wss_urls = [_POLYGON_WSS_PRIMARY, _POLYGON_WSS_FALLBACK]
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
                    logger.info("MorphoPolygon: WS newHeads subscrito @ %s (id=%s)",
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
                logger.warning("MorphoPolygon: WS erro @ %s — reconecta em 5s: %s",
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

        if block_num % _SCAN_INTERVAL_BLOCKS == 0 or not self._positions:
            self._scan_borrowers()
        if not self._positions:
            return []

        if not self.dry_run:
            _pk_chk = self._wallet_pool.primary_pk or get_env("BSC_PRIVATE_KEY") or ""
            _acct_chk = self.w3.eth.account.from_key(_pk_chk)
            try:
                _bal_wei = self.w3.eth.get_balance(_acct_chk.address)
            except Exception as exc:
                logger.warning("MorphoPolygon: get_balance falhou — tick saltado: %s", exc)
                return []
            if _bal_wei < Web3.to_wei(1.0, 'ether'):
                logger.error(
                    "MorphoPolygon: saldo insuficiente (%.4f MATIC < 1.0) — execução suspensa",
                    float(Web3.from_wei(_bal_wei, 'ether')),
                )
                return []

        _now_tick = time.time()
        logger.info("MorphoPolygon Tick: bloco=%d %d posições | %d blacklist",
                    block_num, len(self._positions), len(self._blacklist))

        # ── Fase 1: verificar posições, recolher liquidáveis ─────────────────
        opp_list: list[tuple] = []  # (opp, _cd_key, is_liq, should_exec)
        checked = 0

        for (market_id, borrower) in list(self._positions.keys()):
            if checked >= self.max_per_tick:
                break
            checked += 1
            _cd_key = f"{market_id[:10]}:{borrower.lower()}"

            opp = self._check_position(market_id, borrower)
            if opp is None:
                continue

            if _cd_key in self._blacklist:
                _bl_hf = self._blacklist[_cd_key]
                if abs(opp.health_factor - _bl_hf) / max(_bl_hf, 0.001) < 0.05:
                    logger.debug("MorphoPolygon: %s blacklisted — saltado", borrower[:10] + "…")
                    continue
                del self._blacklist[_cd_key]
                self._fail_counts.pop(_cd_key, None)
                logger.info("MorphoPolygon: %s saiu da blacklist", borrower[:10] + "…")

            is_liq = opp.health_factor < 1.0
            if is_liq:
                logger.info(
                    "MorphoPolygon: LIQUIDÁVEL %s %s/%s HF=%.4f dívida=$%.2f lucro≈$%.4f dry=%s",
                    borrower[:10] + "…", opp.collateral_symbol, opp.loan_symbol,
                    opp.health_factor, opp.borrow_usd, opp.estimated_profit_usd, self.dry_run,
                )
            else:
                logger.debug(
                    "MorphoPolygon: vigiar %s %s/%s HF=%.4f dívida=$%.2f",
                    borrower[:10] + "…", opp.collateral_symbol, opp.loan_symbol,
                    opp.health_factor, opp.borrow_usd,
                )

            should_exec = (
                is_liq
                and not self.dry_run
                and opp.estimated_profit_usd >= self.min_profit
                and self._cooldown.get(_cd_key, 0) <= _now_tick
            )
            if is_liq and not self.dry_run and self._cooldown.get(_cd_key, 0) > _now_tick:
                logger.debug("MorphoPolygon: %s em cooldown (%.0fs)", borrower[:10] + "…",
                             self._cooldown[_cd_key] - _now_tick)
            opp_list.append((opp, _cd_key, is_liq, should_exec))

        # ── Fase 2: bundle attempt + fallback individual ──────────────────────
        tx_map: dict[str, str | None] = {}
        bundled: set[str] = set()
        to_exec = sorted(
            [(opp, cd) for opp, cd, _, se in opp_list if se],
            key=lambda x: -x[0].estimated_profit_usd,
        )
        if to_exec:
            logger.info("MorphoPolygon: %d liquidações prontas", len(to_exec))
            if len(to_exec) >= 2:
                bundled = self._try_bundle(to_exec)
                for _cd in bundled:
                    tx_map[_cd] = "bundle"

            remaining = [(opp, cd) for opp, cd in to_exec if cd not in bundled]
            if remaining:
                def _exec_task(pair):
                    opp, cd = pair
                    try:
                        with self._wallet_pool.borrow(timeout=1.5) as _pk:
                            return cd, self._execute_live(opp, pk=_pk)
                    except Exception as _e:
                        logger.warning("MorphoPolygon: wallet indisponível para %s: %s",
                                       cd[:10], _e)
                        return cd, None

                with ThreadPoolExecutor(max_workers=self._wallet_pool.size) as _ex:
                    for _cd, _tx in _ex.map(_exec_task, remaining):
                        tx_map[_cd] = _tx

        # ── Fase 3: resultados + state update ────────────────────────────────
        results: list[dict] = []
        for opp, _cd_key, is_liq, should_exec in opp_list:
            tx_hash = tx_map.get(_cd_key) if should_exec else None
            if should_exec:
                if tx_hash:
                    self._fail_counts.pop(_cd_key, None)
                    self.notifier.notify(
                        "trade_executed",
                        f"🟣 Morpho Polygon LIQUIDATE {opp.borrower[:10]}… "
                        f"{opp.collateral_symbol}/{opp.loan_symbol} "
                        f"lucro≈${opp.estimated_profit_usd:.2f} | tx={tx_hash[:20]}…",
                    )
                else:
                    _cnt = self._fail_counts.get(_cd_key, 0) + 1
                    self._fail_counts[_cd_key] = _cnt
                    if _cnt >= _BLACKLIST_FAILS:
                        self._blacklist[_cd_key] = opp.health_factor
                        logger.warning("MorphoPolygon: %s adicionado à blacklist (%d falhas)",
                                       opp.borrower[:10] + "…", _cnt)
            self._record(opp)
            results.append({
                "borrower":     opp.borrower,
                "market_id":    _cd_key.split(":")[0],
                "loan":         opp.loan_symbol,
                "collateral":   opp.collateral_symbol,
                "hf":           opp.health_factor,
                "liquidatable": is_liq,
                "borrow_usd":   opp.borrow_usd,
                "profit_usd":   opp.estimated_profit_usd,
                "executed":     tx_hash is not None,
            })

        liquidable = sum(1 for r in results if r["liquidatable"])
        logger.info(
            "MorphoPolygon: tick — %d liquidáveis / %d em vigilância "
            "(%d checados, %d posições, %d mercados)",
            liquidable, len(results) - liquidable, checked,
            len(self._positions), len(self._markets),
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
    print("MORPHO BLUE POLYGON LIQUIDATOR — Smoke Test")
    print("=" * 60)

    bot = MorphoLiquidatorPolygonBot()

    connected = bot._connected()
    print(f"\n[1] RPC Polygon conectado: {connected}")
    if not connected:
        sys.exit(0)

    try:
        bloco = bot.w3.eth.block_number
        print(f"[2] Bloco actual Polygon: {bloco:,}")
    except Exception as exc:
        print(f"[2] Bloco: ERRO — {exc}")
        sys.exit(1)

    print(f"\n[3] Preço MATIC:  ${bot._matic_price():.4f}")
    print(f"    Gas price:    {bot._gas_price_gwei():.2f} Gwei")
    gas_usd = _GAS_UNITS * bot._gas_price_gwei() * 1e-9 * bot._matic_price()
    print(f"    Gas custo:    ${gas_usd:.6f} USD")

    orig = bot.scan_blocks
    bot.scan_blocks = 5000
    print(f"\n[4] A procurar posições (últimos 5000 blocos)…")
    bot._scan_borrowers()
    print(f"    Posições encontradas: {len(bot._positions)}")
    print(f"    Mercados descobertos: {len(bot._markets)}")
    bot.scan_blocks = orig

    print(f"\n[5] A verificar posições (threshold HF={bot.hf_threshold})…")
    found = 0
    for (market_id, borrower) in list(bot._positions.keys())[:20]:
        opp = bot._check_position(market_id, borrower)
        if opp is None:
            continue
        status = "⚠️  LIQUIDÁVEL" if opp.health_factor < 1.0 else f"HF={opp.health_factor:.4f}"
        print(f"    {borrower[:16]}…  {opp.collateral_symbol}/{opp.loan_symbol}"
              f"  dívida=${opp.borrow_usd:.2f}  {status}")
        if opp.health_factor < 1.0:
            print(f"       lucro≈${opp.estimated_profit_usd:.4f}  "
                  f"LIF={opp.lif_pct:.2f}%  gas≈${opp.gas_cost_usd:.6f}")
            found += 1
    print(f"    Total liquidáveis: {found} (dry_run={bot.dry_run})")

    print("\nSMOKE OK")
