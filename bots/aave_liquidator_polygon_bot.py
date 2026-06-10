"""Bot de liquidações Aave V3 na chain Polygon.

Estratégia idêntica ao aave_liquidator_bot.py (Base), mas independente:
  1. Mantém lista de mutuários via scanning de eventos Borrow
  2. Verifica health factor de cada posição a cada poll_seconds
  3. Posições com HF < health_factor_threshold (default: 1.0):
       – Calcula dívida a cobrir (50% do total, limite Aave)
       – Estima colateral a receber (dívida × (1 + bonus))
       – Estima custo de gas (Polygon usa MATIC)
       – Calcula lucro líquido em USD
  4. DRY_RUN=true: regista oportunidade na BD, NÃO executa
  5. DRY_RUN=false: chama contrato de flash loan para executar
       (requer flash_loan_contract em settings.yaml aave_liquidator_polygon)

Contratos Aave V3 Polygon (mainnet, chain 137):
  Pool:        0x794a61358D6845594F94dc1DB02A252b5b4814aD
  PriceOracle: 0xb023e699F5a33916Ea823A16485e259257cA8Bd1

RPC: ALCHEMY_POLYGON_URL do .env (fallback: https://polygon-rpc.com)
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import requests
from web3 import Web3
from web3.exceptions import ContractLogicError

from utils.config import get_env, get_settings
from utils.wallet_pool import WalletPool
from utils.database import init_db, upsert_liquidation_opportunity
from utils.flashbots import send_bundle as _fb_send_bundle
from utils.flashbots import send_bundle_multi as _fb_send_multi
from utils.logger import get_logger
from utils.notifier import TelegramNotifier

logger = get_logger(__name__)

# ── Aave V3 Polygon — endereços ──────────────────────────────────────────────

POOL_ADDRESS          = Web3.to_checksum_address("0x794a61358D6845594F94dc1DB02A252b5b4814aD")
ORACLE_ADDRESS        = Web3.to_checksum_address("0xb023e699F5a33916Ea823A16485e259257cA8Bd1")
DATA_PROVIDER_ADDRESS = Web3.to_checksum_address("0x243Aa95cAC2a25651eda86e80bEe66114413c43b")
POLYGON_CHAIN_ID      = 137
WMATIC_POLYGON    = "0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270"  # gas token para custo USD
_POLYGON_FALLBACK_RPC = "https://polygon.drpc.org"

# Bonus de liquidação Aave V3 Polygon (valores conservadores)
_BONUS: dict[str, float] = {
    "0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270": 0.050,  # WMATIC  → 5%
    "0x7ceb23fd6bc0add59e62ac25578270cff1b9f619": 0.050,  # WETH    → 5%
    "0x2791bca1f2de4661ed88a30c99a7a9449aa84174": 0.050,  # USDC.e  → 5%
    "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359": 0.050,  # USDC    → 5%
    "0xc2132d05d31c914a87c6611c10748aeb04b58e8f": 0.050,  # USDT    → 5%
    "0x8f3cf7ad23cd3cadbd9735aff958023239c6a063": 0.050,  # DAI     → 5%
    "0x03b54a6e9a984069379fae1a4fc4dbae93b3bccd": 0.070,  # wstETH  → 7%
    "0xfa68fb4628dff1028cfec22b4162fccd0d45efb6": 0.075,  # MaticX  → 7.5%
    "0x4e3decbb3645551b8a19f0ea1678079fcb33fb4c": 0.075,  # stMATIC → 7.5%
}
_DEFAULT_BONUS    = 0.050  # 5% conservador

_HF_LIQUIDATABLE = 1.0      # Aave V3: só liquidável quando HF < 1.0 (monitorização em 1.2)

_FLASHBOTS_ENDPOINT      = "https://polygon.flashbots.net"
_FLASHBOTS_MIN_PROFIT_USD = 200.0
_DEBT_MIN_USD    = 500.0
_DEBT_MAX_USD    = 50_000.0
_BLACKLIST_FAILS = 3
_MAX_BUNDLE_TXS  = 4

_TOKEN_SYMBOLS: dict[str, str] = {
    "0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270": "WMATIC",
    "0x7ceb23fd6bc0add59e62ac25578270cff1b9f619": "WETH",
    "0x2791bca1f2de4661ed88a30c99a7a9449aa84174": "USDC.e",
    "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359": "USDC",
    "0xc2132d05d31c914a87c6611c10748aeb04b58e8f": "USDT",
    "0x8f3cf7ad23cd3cadbd9735aff958023239c6a063": "DAI",
    "0x1bfd67037b42cf73acf2047067bd4f2c47d9bfd6": "WBTC",
}

_GAS_UNITS        = 500_000   # estimativa flash loan + liquidação
_POOL_FEES_POLYGON: dict[str, int] = {
    "0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270": 500,   # WMATIC
    "0x7ceb23fd6bc0add59e62ac25578270cff1b9f619": 500,   # WETH
    "0x2791bca1f2de4661ed88a30c99a7a9449aa84174": 100,   # USDC.e
    "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359": 100,   # USDC
    "0xc2132d05d31c914a87c6611c10748aeb04b58e8f": 100,   # USDT
    "0x8f3cf7ad23cd3cadbd9735aff958023239c6a063": 100,   # DAI
}
_ORACLE_DECIMALS  = 8         # Aave oracle: USD com 8 decimais
_HF_DECIMALS      = 18        # healthFactor em ray (1e18)
_ACCOUNT_DECIMALS = 8         # totalCollateralBase/totalDebtBase em USD×1e8

MULTICALL3_ADDRESS = Web3.to_checksum_address("0xcA11bde05977b3631167028862bE2a173976CA11")
_MULTICALL3_CHUNK  = 500  # max calls por aggregate3

_ACCOUNT_DATA_TYPES = (
    "uint256",  # totalCollateralBase
    "uint256",  # totalDebtBase
    "uint256",  # availableBorrowsBase
    "uint256",  # currentLiquidationThreshold
    "uint256",  # ltv
    "uint256",  # healthFactor
)
_RESERVE_DATA_TYPES = (
    "uint256", "uint256", "uint256", "uint256",
    "uint256", "uint256", "uint256", "uint40", "bool",
)

# ── ABIs mínimos ─────────────────────────────────────────────────────────────

_POOL_ABI = [
    {
        "inputs": [{"name": "user", "type": "address"}],
        "name": "getUserAccountData",
        "outputs": [
            {"name": "totalCollateralBase",          "type": "uint256"},
            {"name": "totalDebtBase",                "type": "uint256"},
            {"name": "availableBorrowsBase",         "type": "uint256"},
            {"name": "currentLiquidationThreshold",  "type": "uint256"},
            {"name": "ltv",                          "type": "uint256"},
            {"name": "healthFactor",                 "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "getReservesList",
        "outputs": [{"name": "", "type": "address[]"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "asset", "type": "address"},
            {"name": "user",  "type": "address"},
        ],
        "name": "getUserReserveData",
        "outputs": [
            {"name": "currentATokenBalance",     "type": "uint256"},
            {"name": "currentStableDebt",        "type": "uint256"},
            {"name": "currentVariableDebt",      "type": "uint256"},
            {"name": "principalStableDebt",      "type": "uint256"},
            {"name": "scaledVariableDebt",       "type": "uint256"},
            {"name": "stableBorrowRate",         "type": "uint256"},
            {"name": "liquidityRate",            "type": "uint256"},
            {"name": "stableRateLastUpdated",    "type": "uint40"},
            {"name": "usageAsCollateralEnabled", "type": "bool"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True,  "name": "reserve",          "type": "address"},
            {"indexed": False, "name": "user",             "type": "address"},
            {"indexed": True,  "name": "onBehalfOf",       "type": "address"},
            {"indexed": False, "name": "amount",           "type": "uint256"},
            {"indexed": False, "name": "interestRateMode", "type": "uint8"},
            {"indexed": False, "name": "borrowRate",       "type": "uint256"},
            {"indexed": True,  "name": "referralCode",     "type": "uint16"},
        ],
        "name": "Borrow",
        "type": "event",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True,  "name": "collateralAsset",            "type": "address"},
            {"indexed": True,  "name": "debtAsset",                  "type": "address"},
            {"indexed": True,  "name": "user",                       "type": "address"},
            {"indexed": False, "name": "debtToCover",                "type": "uint256"},
            {"indexed": False, "name": "liquidatedCollateralAmount",  "type": "uint256"},
            {"indexed": False, "name": "liquidator",                 "type": "address"},
            {"indexed": False, "name": "receiveAToken",              "type": "bool"},
        ],
        "name": "LiquidationCall",
        "type": "event",
    },
]

_ORACLE_ABI = [
    {
        "inputs": [{"name": "asset", "type": "address"}],
        "name": "getAssetPrice",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

_ERC20_DECIMALS_ABI = [
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    }
]

_FLASH_LIQ_ABI = [
    {
        "inputs": [
            {"name": "debtAsset",       "type": "address"},
            {"name": "collateralAsset", "type": "address"},
            {"name": "borrower",        "type": "address"},
            {"name": "debtAmount",      "type": "uint256"},
            {"name": "poolFee",         "type": "uint24"},
        ],
        "name": "executeFlashLiquidation",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]

_MULTICALL3_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {"name": "target",       "type": "address"},
                    {"name": "allowFailure", "type": "bool"},
                    {"name": "callData",     "type": "bytes"},
                ],
                "name": "calls",
                "type": "tuple[]",
            }
        ],
        "name": "aggregate3",
        "outputs": [
            {
                "components": [
                    {"name": "success",    "type": "bool"},
                    {"name": "returnData", "type": "bytes"},
                ],
                "name": "returnData",
                "type": "tuple[]",
            }
        ],
        "stateMutability": "payable",
        "type": "function",
    }
]


@dataclass
class LiqOpportunityPolygon:
    borrower: str
    health_factor: float
    total_collateral_usd: float
    total_debt_usd: float
    debt_to_cover_usd: float
    collateral_to_receive_usd: float
    liquidation_bonus_pct: float
    gas_cost_usd: float
    net_profit_usd: float
    debt_asset: str      = field(default="")
    collateral_asset: str = field(default="")


# ── Bot ───────────────────────────────────────────────────────────────────────

class AaveLiquidatorPolygonBot:
    """Monitoriza e (em modo live) executa liquidações Aave V3 na Polygon chain."""

    def __init__(self, settings: dict | None = None):
        self.settings = settings or get_settings()
        self.cfg = self.settings.get("bots", {}).get("aave_liquidator_polygon", {})

        primary_rpc = get_env("ALCHEMY_POLYGON_URL") or _POLYGON_FALLBACK_RPC
        self._rpc_urls: list[str] = [primary_rpc, _POLYGON_FALLBACK_RPC]
        self._active_rpc: str = primary_rpc

        self.dry_run      : bool  = False
        self.hf_threshold : float = float(self.cfg.get("health_factor_threshold", 1.0))
        self.min_profit   : float = float(self.cfg.get("min_profit_usd", 5.0))
        self.scan_blocks  : int   = int(self.cfg.get("borrower_scan_blocks", 50_000))
        self.max_per_tick : int   = int(self.cfg.get("max_positions_per_tick", 50))

        flash_addr = get_env("FLASH_LOAN_CONTRACT_POLYGON") or self.cfg.get("flash_loan_contract", "")

        self.w3     = Web3(Web3.HTTPProvider(primary_rpc, request_kwargs={"timeout": 30}))
        self.pool   = self.w3.eth.contract(address=POOL_ADDRESS,   abi=_POOL_ABI)
        self.oracle = self.w3.eth.contract(address=ORACLE_ADDRESS, abi=_ORACLE_ABI)
        self.flash  = (
            self.w3.eth.contract(
                address=Web3.to_checksum_address(flash_addr), abi=_FLASH_LIQ_ABI
            ) if flash_addr else None
        )
        self.multicall     = self.w3.eth.contract(address=MULTICALL3_ADDRESS,    abi=_MULTICALL3_ABI)
        self.data_provider = self.w3.eth.contract(address=DATA_PROVIDER_ADDRESS, abi=_POOL_ABI)

        self._borrowers: set[str] = set()
        self._scan_from: int = 0
        self._reserves : list[str] = []
        self._matic_price_cache: float = 0.80
        self._matic_price_ts   : float = 0.0
        self._decimals_cache   : dict[str, int] = {}
        self._price_cache      : dict[str, int] = {}
        self._cooldown         : dict[str, float] = {}
        self._fail_counts      : dict[str, int]   = {}
        self._blacklist        : dict[str, float] = {}

        self.notifier = TelegramNotifier(self.settings)
        self._wallet_pool = WalletPool()
        init_db()

        if not flash_addr:
            self.dry_run = True
            logger.warning(
                "AavePolygon: flash_loan_contract não configurado — modo DRY_RUN forçado para live"
            )

        logger.info(
            "AavePolygon: rpc=%s dry_run=%s hf<%.2f min_profit=$%.2f",
            primary_rpc.split("//")[-1].split("/")[0], self.dry_run,
            self.hf_threshold, self.min_profit,
        )

    # ── helpers ──────────────────────────────────────────────────────────────

    def _switch_rpc(self, failed_url: str) -> bool:
        """Troca para o próximo RPC disponível quando o actual falha."""
        for url in self._rpc_urls:
            if url == failed_url:
                continue
            try:
                test_w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 15}))
                test_w3.eth.block_number
                self.w3     = test_w3
                self._active_rpc = url
                self.pool   = self.w3.eth.contract(address=POOL_ADDRESS,   abi=_POOL_ABI)
                self.oracle = self.w3.eth.contract(address=ORACLE_ADDRESS, abi=_ORACLE_ABI)
                if self.flash is not None:
                    self.flash = self.w3.eth.contract(
                        address=self.flash.address, abi=_FLASH_LIQ_ABI
                    )
                self.multicall     = self.w3.eth.contract(address=MULTICALL3_ADDRESS,    abi=_MULTICALL3_ABI)
                self.data_provider = self.w3.eth.contract(address=DATA_PROVIDER_ADDRESS, abi=_POOL_ABI)
                logger.warning("AavePolygon: RPC trocado para fallback: %s",
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
                            "AavePolygon: rate-limit (429) — a tentar fallback RPC (tentativa %d/3)",
                            attempt + 1,
                        )
                        if self._switch_rpc(self._active_rpc):
                            continue
                    wait = 2 ** attempt
                    logger.debug("AavePolygon: 429 rate-limit, aguardar %ds…", wait)
                    time.sleep(wait)
                else:
                    logger.debug("AavePolygon: HTTP %s ao verificar ligação: %s", status, exc)
                    return False
            except Exception as exc:
                logger.debug("AavePolygon: erro ao verificar ligação RPC: %s", exc)
                if attempt == 0 and self._active_rpc != _POLYGON_FALLBACK_RPC:
                    if self._switch_rpc(self._active_rpc):
                        continue
                return False
        return False

    def _matic_price(self) -> float:
        now = time.time()
        if now - self._matic_price_ts < 300:
            return self._matic_price_cache
        try:
            raw = self.oracle.functions.getAssetPrice(
                Web3.to_checksum_address(WMATIC_POLYGON)).call()
            self._matic_price_cache = raw / 10 ** _ORACLE_DECIMALS
            self._matic_price_ts    = now
        except Exception as exc:
            logger.debug("AavePolygon: oracle MATIC price falhou: %s — cache $%.4f",
                         exc, self._matic_price_cache)
        return self._matic_price_cache

    def _gas_price_gwei(self) -> float:
        try:
            return self.w3.eth.gas_price / 1e9
        except Exception:
            return 30.0   # Polygon: ~30 Gwei típico

    def _calc_gas_price(self, net_profit_usd: float) -> int:
        """Priority tip em 3 tiers baseado no lucro estimado. Devolve gasPrice em Wei."""
        try:
            base_fee = self.w3.eth.get_block("latest")["baseFeePerGas"]
        except Exception:
            base_fee = int(30 * 1e9)  # 30 gwei fallback (Polygon)
        if net_profit_usd < 50:
            tip_gwei = 1.5
        elif net_profit_usd < 500:
            tip_gwei = 7.5
        else:
            tip_gwei = 25.0
        gas_price = base_fee + int(tip_gwei * 1e9)
        logger.info(
            "AavePolygon: gas_price=%.4f gwei (base=%.4f + tip=%.1f) lucro≈$%.2f",
            gas_price / 1e9, base_fee / 1e9, tip_gwei, net_profit_usd,
        )
        return gas_price

    def _token_decimals(self, token: str) -> int:
        key = token.lower()
        if key not in self._decimals_cache:
            try:
                erc = self.w3.eth.contract(
                    address=Web3.to_checksum_address(token),
                    abi=_ERC20_DECIMALS_ABI,
                )
                self._decimals_cache[key] = erc.functions.decimals().call()
            except Exception:
                self._decimals_cache[key] = 18
        return self._decimals_cache[key]

    def _get_asset_price(self, token: str) -> int:
        key = token.lower()
        if key not in self._price_cache:
            self._price_cache[key] = self.oracle.functions.getAssetPrice(
                Web3.to_checksum_address(token)).call()
        return self._price_cache[key]

    def _batch_account_data(self, borrowers: list[str]) -> dict[str, dict]:
        """getUserAccountData de N borrowers → 1 Multicall3. Devolve {} se falhar."""
        from eth_abi import decode as abi_decode
        if not borrowers:
            return {}
        calls = [
            (POOL_ADDRESS, True,
             self.pool.encode_abi("getUserAccountData",
                                 args=[Web3.to_checksum_address(b)]))
            for b in borrowers
        ]
        raw: list[tuple[bool, bytes]] = []
        for i in range(0, len(calls), _MULTICALL3_CHUNK):
            chunk = calls[i : i + _MULTICALL3_CHUNK]
            try:
                raw.extend(self.multicall.functions.aggregate3(chunk).call())
            except Exception as exc:
                logger.warning("AavePolygon: Multicall3 account_data chunk [%d] falhou: %s", i, exc)
                raw.extend([(False, b"")] * len(chunk))
        result: dict[str, dict] = {}
        for b, (success, ret) in zip(borrowers, raw):
            if not success or len(ret) < 32:
                continue
            try:
                vals = abi_decode(_ACCOUNT_DATA_TYPES, ret)
                if vals[1] == 0:
                    continue
                result[b.lower()] = {
                    "hf":       vals[5] / 10 ** _HF_DECIMALS,
                    "col_usd":  vals[0] / 10 ** _ACCOUNT_DECIMALS,
                    "debt_usd": vals[1] / 10 ** _ACCOUNT_DECIMALS,
                }
            except Exception:
                continue
        logger.info("AavePolygon: Multicall3 account_data: %d borrowers → %d com dívida",
                    len(borrowers), len(result))
        return result

    def _batch_reserve_data(
        self, borrowers: list[str], reserves: list[str]
    ) -> dict[str, dict]:
        """getUserReserveData N×M → aggregate3 em chunks de 500."""
        from eth_abi import decode as abi_decode
        if not borrowers or not reserves:
            return {}
        calls: list[tuple] = []
        index_map: list[tuple[str, str]] = []
        for b in borrowers:
            for r in reserves:
                calls.append((
                    DATA_PROVIDER_ADDRESS, True,
                    self.data_provider.encode_abi("getUserReserveData",
                                                  args=[Web3.to_checksum_address(r),
                                                        Web3.to_checksum_address(b)]),
                ))
                index_map.append((b.lower(), r))
        raw: list[tuple[bool, bytes]] = []
        for i in range(0, len(calls), _MULTICALL3_CHUNK):
            chunk = calls[i : i + _MULTICALL3_CHUNK]
            try:
                raw.extend(self.multicall.functions.aggregate3(chunk).call())
            except Exception as exc:
                logger.warning("AavePolygon: Multicall3 reserve_data chunk [%d] falhou: %s", i, exc)
                raw.extend([(False, b"")] * len(chunk))
        result: dict[str, dict] = {}
        for (borrower, reserve), (success, ret) in zip(index_map, raw):
            if not success or len(ret) < 32:
                continue
            try:
                vals       = abi_decode(_RESERVE_DATA_TYPES, ret)
                a_bal      = vals[0]
                var_debt   = vals[2]
                use_as_col = vals[8]
                price      = self._get_asset_price(reserve)
                debt_val   = var_debt * price
                col_val    = a_bal * price if use_as_col else 0
                entry = result.setdefault(borrower, {
                    "best_debt_val": 0, "debt_asset": WMATIC_POLYGON,
                    "best_col_val":  0, "col_asset":  WMATIC_POLYGON,
                })
                if debt_val > entry["best_debt_val"]:
                    entry["best_debt_val"] = debt_val
                    entry["debt_asset"]    = reserve
                if col_val > entry["best_col_val"]:
                    entry["best_col_val"] = col_val
                    entry["col_asset"]    = reserve
            except Exception:
                continue
        n_chunks = max(1, (len(calls) + _MULTICALL3_CHUNK - 1) // _MULTICALL3_CHUNK)
        logger.info("AavePolygon: Multicall3 reserve_data: %d×%d=%d calls em %d chunk(s) → %d resultados",
                    len(borrowers), len(reserves), len(calls), n_chunks, len(result))
        return result

    def _reserves_list(self) -> list[str]:
        if not self._reserves:
            try:
                self._reserves = [
                    Web3.to_checksum_address(r)
                    for r in self.pool.functions.getReservesList().call()
                ]
                logger.info("AavePolygon: Reserves Aave Polygon: %d activos", len(self._reserves))
            except Exception as exc:
                logger.warning("AavePolygon: getReservesList falhou: %s", exc)
        return self._reserves

    # ── descoberta de mutuários ───────────────────────────────────────────────

    def _scan_borrowers(self) -> None:
        """Varre eventos Borrow para encontrar mutuários activos."""
        if not self._connected():
            return
        try:
            latest = self.w3.eth.block_number
        except Exception as exc:
            logger.warning("AavePolygon: não consegui ler bloco actual: %s", exc)
            return

        from_block = self._scan_from or max(0, latest - self.scan_blocks)
        chunk, added = 2000, 0

        while from_block < latest:
            to_block = min(from_block + chunk - 1, latest)
            try:
                events = self.pool.events.Borrow().get_logs(
                    from_block=from_block, to_block=to_block)
                for e in events:
                    addr = e.args["onBehalfOf"].lower()
                    if addr not in self._borrowers:
                        self._borrowers.add(addr)
                        added += 1
                from_block = to_block + 1
            except Exception as exc:
                logger.debug("AavePolygon: Borrow events [%d..%d]: %s", from_block, to_block, exc)
                break

        self._scan_from = latest

        try:
            liq_start = max(0, latest - 5_000)
            liq_evts  = self.pool.events.LiquidationCall().get_logs(
                from_block=liq_start, to_block=latest)
            for e in liq_evts:
                self._borrowers.discard(e.args["user"].lower())
        except Exception:
            pass

        if added:
            logger.info("AavePolygon: Scan Borrow: +%d mutuários (total=%d, bloco=%d)",
                        added, len(self._borrowers), latest)

    # ── análise de posições ───────────────────────────────────────────────────

    def _check_health(self, address: str) -> tuple[float, float, float] | None:
        """Devolve (health_factor, collateral_usd, debt_usd) ou None."""
        try:
            (col_raw, debt_raw, _, _, _, hf_raw) = self.pool.functions.getUserAccountData(
                Web3.to_checksum_address(address)).call()
            if debt_raw == 0:
                return None
            return (
                hf_raw   / 10 ** _HF_DECIMALS,
                col_raw  / 10 ** _ACCOUNT_DECIMALS,
                debt_raw / 10 ** _ACCOUNT_DECIMALS,
            )
        except (ContractLogicError, Exception) as exc:
            logger.debug("AavePolygon: getUserAccountData(%s…): %s", address[:10], exc)
            return None

    def _estimate(self, borrower: str, hf: float,
                  col_usd: float, debt_usd: float,
                  _pre: dict | None = None) -> LiqOpportunityPolygon:
        if _pre is not None:
            entry            = _pre.get(borrower.lower(), {})
            debt_asset       = entry.get("debt_asset", WMATIC_POLYGON)
            collateral_asset = entry.get("col_asset",  WMATIC_POLYGON)
        else:
            # fallback sequencial (Multicall3 falhou totalmente)
            reserves = self._reserves_list()
            debt_asset = collateral_asset = WMATIC_POLYGON
            best_debt_val = best_col_val = 0
            for reserve in reserves:
                try:
                    (a_bal, _, var_debt, _, _, _, _, _, use_as_col) = \
                        self.data_provider.functions.getUserReserveData(
                            Web3.to_checksum_address(reserve),
                            Web3.to_checksum_address(borrower),
                        ).call()
                    price    = self._get_asset_price(reserve)
                    debt_val = var_debt * price
                    col_val  = a_bal * price if use_as_col else 0
                    if debt_val > best_debt_val:
                        best_debt_val = debt_val
                        debt_asset    = reserve
                    if col_val > best_col_val:
                        best_col_val     = col_val
                        collateral_asset = reserve
                except Exception:
                    continue

        bonus          = _BONUS.get(collateral_asset.lower(), _DEFAULT_BONUS)
        debt_to_cover  = debt_usd * 0.50
        col_to_receive = debt_to_cover * (1.0 + bonus)
        gas_usd        = _GAS_UNITS * self._gas_price_gwei() * 1e-9 * self._matic_price()
        net_profit     = col_to_receive - debt_to_cover - gas_usd

        return LiqOpportunityPolygon(
            borrower=borrower,
            health_factor=round(hf, 6),
            total_collateral_usd=round(col_usd, 2),
            total_debt_usd=round(debt_usd, 2),
            debt_to_cover_usd=round(debt_to_cover, 4),
            collateral_to_receive_usd=round(col_to_receive, 4),
            liquidation_bonus_pct=round(bonus * 100, 1),
            gas_cost_usd=round(gas_usd, 6),
            net_profit_usd=round(net_profit, 4),
            debt_asset=debt_asset,
            collateral_asset=collateral_asset,
        )

    # ── execução / registo ────────────────────────────────────────────────────

    def _record(self, opp: LiqOpportunityPolygon, executed: bool,
                tx_hash: str | None = None) -> None:
        rec_id, inserted = upsert_liquidation_opportunity(
            position_address=opp.borrower,
            health_factor=opp.health_factor,
            debt_asset=opp.debt_asset,
            debt_amount_usd=opp.debt_to_cover_usd,
            collateral_asset=opp.collateral_asset,
            collateral_amount_usd=opp.collateral_to_receive_usd,
            liquidation_bonus_pct=opp.liquidation_bonus_pct,
            estimated_profit_usd=opp.net_profit_usd,
            gas_cost_usd=opp.gas_cost_usd,
            executed=executed,
            tx_hash=tx_hash,
            dry_run=self.dry_run,
            status="dry_run" if self.dry_run else ("executed" if executed else "skipped"),
            chain="polygon",
        )
        action = "INSERT" if inserted else "UPDATE"
        logger.debug("AavePolygon: BD %s id=%d %s HF=%.4f",
                     action, rec_id, opp.borrower[:10], opp.health_factor)

    def _execute_live(self, opp: LiqOpportunityPolygon, *, pk: str | None = None) -> str | None:
        """Executa flash loan liquidation via contrato deployado."""
        if self.flash is None:
            logger.error(
                "AavePolygon: flash_loan_contract não configurado — não é possível executar live"
            )
            return None
        try:
            pk   = pk or get_env("BSC_PRIVATE_KEY") or ""
            acct = self.w3.eth.account.from_key(pk)
            debt_oracle = self._get_asset_price(opp.debt_asset)
            token_dec   = self._token_decimals(opp.debt_asset)
            debt_units  = int(opp.debt_to_cover_usd
                              / (debt_oracle / 10 ** _ORACLE_DECIMALS)
                              * 10 ** token_dec)

            pool_fee = _POOL_FEES_POLYGON.get(opp.collateral_asset.lower(), 3000)

            # Simulação obrigatória: eth_call antes de enviar TX (zero gas se falhar)
            try:
                self.flash.functions.executeFlashLiquidation(
                    Web3.to_checksum_address(opp.debt_asset),
                    Web3.to_checksum_address(opp.collateral_asset),
                    Web3.to_checksum_address(opp.borrower),
                    debt_units,
                    pool_fee,
                ).call({"from": acct.address})
            except Exception as sim_exc:
                self._cooldown[opp.borrower.lower()] = time.time() + 600
                logger.warning(
                    "AavePolygon: simulação falhou %s — cooldown 10min: %s",
                    opp.borrower[:10] + "…", sim_exc,
                )
                return None

            tx = self.flash.functions.executeFlashLiquidation(
                Web3.to_checksum_address(opp.debt_asset),
                Web3.to_checksum_address(opp.collateral_asset),
                Web3.to_checksum_address(opp.borrower),
                debt_units,
                pool_fee,
            ).build_transaction({
                "from":     acct.address,
                "chainId":  POLYGON_CHAIN_ID,
                "gas":      _GAS_UNITS,
                "gasPrice": self._calc_gas_price(opp.net_profit_usd),
                "nonce":    self.w3.eth.get_transaction_count(acct.address, 'pending'),
            })
            signed = acct.sign_transaction(tx)
            if opp.net_profit_usd >= _FLASHBOTS_MIN_PROFIT_USD:
                try:
                    _tgt = self.w3.eth.block_number + 1
                    _bh = _fb_send_bundle(
                        "0x" + signed.raw_transaction.hex(),
                        _tgt, _FLASHBOTS_ENDPOINT, pk,
                    )
                    if _bh:
                        _exp = Web3.keccak(primitive=bytes(signed.raw_transaction))
                        logger.info("AavePolygon: TX via Flashbots @ bloco %d: %s…", _tgt, _exp.hex()[:18])
                        return _exp.hex()
                except Exception as _fb_exc:
                    logger.warning("AavePolygon: Flashbots falhou — fallback mempool: %s", _fb_exc)
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            logger.info("AavePolygon: LIQUIDAÇÃO TX: %s", tx_hash.hex())
            return tx_hash.hex()
        except Exception as exc:
            if "HEALTH_FACTOR_NOT_BELOW_THRESHOLD" in str(exc):
                until = time.time() + 600
                self._cooldown[opp.borrower.lower()] = until
                logger.warning(
                    "AavePolygon: %s HF acima do threshold — cooldown 10min",
                    opp.borrower[:10] + "…",
                )
            else:
                logger.error("AavePolygon: falha ao executar liquidação: %s", exc)
            return None

    # ── bundle helpers ────────────────────────────────────────────────────────

    def _sign_tx_nosim(self, opp: LiqOpportunityPolygon, pk: str, nonce: int) -> bytes | None:
        """Build+sign tx without eth_call simulation (for bundle use)."""
        if self.flash is None:
            return None
        try:
            from eth_account import Account as _Acct
            if pk.startswith("0x"):
                pk = pk[2:]
            acct       = _Acct.from_key(pk)
            debt_oracle = self._get_asset_price(opp.debt_asset)
            token_dec   = self._token_decimals(opp.debt_asset)
            debt_units  = int(opp.debt_to_cover_usd
                              / (debt_oracle / 10 ** _ORACLE_DECIMALS)
                              * 10 ** token_dec)
            pool_fee = _POOL_FEES_POLYGON.get(opp.collateral_asset.lower(), 3000)
            tx = self.flash.functions.executeFlashLiquidation(
                Web3.to_checksum_address(opp.debt_asset),
                Web3.to_checksum_address(opp.collateral_asset),
                Web3.to_checksum_address(opp.borrower),
                debt_units,
                pool_fee,
            ).build_transaction({
                "from":     acct.address,
                "chainId":  POLYGON_CHAIN_ID,
                "gas":      _GAS_UNITS,
                "gasPrice": self._calc_gas_price(opp.net_profit_usd),
                "nonce":    nonce,
            })
            signed = _Acct.sign_transaction(tx, pk)
            return bytes(signed.raw_transaction)
        except Exception as exc:
            logger.warning("AavePolygon: _sign_tx_nosim falhou %s: %s",
                           opp.borrower[:10] + "…", exc)
            return None

    def _try_bundle(self, to_exec: list[tuple]) -> set[str]:
        """Sign all txs (no sim) and submit as Flashbots bundle. Returns bundled borrower_lowers."""
        if self.flash is None or len(to_exec) < 2:
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
            raw_txes, b_lows = [], []
            for i, (opp, _b_low) in enumerate(to_exec[:_MAX_BUNDLE_TXS]):
                raw = self._sign_tx_nosim(opp, pk, base_nonce + i)
                if raw is None:
                    continue
                raw_txes.append("0x" + raw.hex())
                b_lows.append(_b_low)
            if len(raw_txes) < 2:
                return set()
            bh = _fb_send_multi(raw_txes, target, _FLASHBOTS_ENDPOINT, pk)
            if bh:
                logger.info("AavePolygon: bundle %d txs @ bloco %d: %s…",
                            len(raw_txes), target, bh[:16])
                return set(b_lows)
            return set()
        except Exception as exc:
            logger.warning("AavePolygon: bundle falhou → fallback individual: %s", exc)
            return set()

    # ── tick ─────────────────────────────────────────────────────────────────

    def tick(self) -> list[dict]:
        if not self._connected():
            logger.warning("AavePolygon: sem ligação ao RPC Polygon — tick saltado")
            return []

        self._scan_borrowers()
        self._price_cache = {}

        candidates = list(self._borrowers)[:self.max_per_tick]
        if not candidates:
            return []

        # Fase 1: getUserAccountData de todos os candidatos → 1 Multicall3
        account_data = self._batch_account_data(candidates)
        if not account_data:
            logger.warning("AavePolygon: Multicall3 account_data falhou — fallback sequencial")
            eligible: list[tuple[str, float, float, float]] = []
            for b in candidates:
                d = self._check_health(b)
                if d is not None and d[0] < self.hf_threshold:
                    eligible.append((b, d[0], d[1], d[2]))
        else:
            eligible = [
                (b, d["hf"], d["col_usd"], d["debt_usd"])
                for b, d in account_data.items()
                if d["hf"] < self.hf_threshold
            ]

        # Filtro de tamanho: só posições com debt entre $500 e $50,000
        eligible = [(b, hf, col, debt) for b, hf, col, debt in eligible
                    if _DEBT_MIN_USD <= debt <= _DEBT_MAX_USD]

        if not eligible:
            logger.debug("AavePolygon: 0 posições elegíveis (%d candidatos)", len(candidates))
            return []

        # Fase 2: getUserReserveData dos elegíveis × reserves → 1-2 Multicall3
        reserves     = self._reserves_list()
        reserve_data = self._batch_reserve_data([b for b, *_ in eligible], reserves)

        # Saldo mínimo: suspende execução se POL insuficiente para gas (verifica carteira primária)
        if not self.dry_run:
            _pk_chk   = self._wallet_pool.primary_pk or get_env("BSC_PRIVATE_KEY") or ""
            _acct_chk = self.w3.eth.account.from_key(_pk_chk)
            _bal_pol  = self.w3.eth.get_balance(_acct_chk.address)
            if _bal_pol < Web3.to_wei(0.3, 'ether'):
                logger.error(
                    "AavePolygon: saldo insuficiente (%.4f POL < 0.3) — execução suspensa",
                    float(Web3.from_wei(_bal_pol, 'ether')),
                )
                return []

        # Sumário do tick
        _now_tick    = time.time()
        _n_liq       = sum(1 for _, hf, _, _ in eligible if hf < _HF_LIQUIDATABLE)
        _n_cooldown  = sum(1 for b, *_ in eligible if self._cooldown.get(b.lower(), 0) > _now_tick)
        _n_blacklist = len(self._blacklist)
        logger.info(
            "AavePolygon Tick: %d elegíveis | %d liquidáveis (HF<1.0) | %d cooldown | %d blacklist",
            len(eligible), _n_liq, _n_cooldown, _n_blacklist,
        )

        # ── Fase 1: estimar todas as oportunidades ────────────────────────────
        _now_exec = time.time()
        opp_list: list[tuple] = []  # (opp, _b_low, should_execute)
        for borrower, hf, col_usd, debt_usd in eligible:
            _b_low = borrower.lower()

            if _b_low in self._blacklist:
                _bl_hf = self._blacklist[_b_low]
                if abs(hf - _bl_hf) / max(_bl_hf, 0.001) < 0.05:
                    logger.debug("AavePolygon: %s blacklisted (HF=%.4f base=%.4f) — saltado",
                                 borrower[:10] + "…", hf, _bl_hf)
                    continue
                del self._blacklist[_b_low]
                self._fail_counts.pop(_b_low, None)
                logger.info("AavePolygon: %s saiu da blacklist (HF %.4f→%.4f)",
                            borrower[:10] + "…", _bl_hf, hf)

            opp = self._estimate(borrower, hf, col_usd, debt_usd,
                                 _pre=reserve_data if reserve_data else None)

            _debt_sym = _TOKEN_SYMBOLS.get(opp.debt_asset.lower(), opp.debt_asset[-6:])
            _col_sym  = _TOKEN_SYMBOLS.get(opp.collateral_asset.lower(), opp.collateral_asset[-6:])
            logger.info(
                "AavePolygon: LIQUIDAÇÃO %s HF=%.4f debt=$%.2f(%s) col=$%.2f(%s) lucro≈$%.4f dry=%s",
                borrower[:10] + "…", hf,
                debt_usd, _debt_sym, col_usd, _col_sym,
                opp.net_profit_usd, self.dry_run,
            )

            should_exec = (
                not self.dry_run
                and hf < _HF_LIQUIDATABLE
                and opp.net_profit_usd >= self.min_profit
                and self._cooldown.get(_b_low, 0) <= _now_exec
            )
            if not should_exec and not self.dry_run and self._cooldown.get(_b_low, 0) > _now_exec:
                logger.debug("AavePolygon: %s em cooldown (%.0fs) — saltado",
                             borrower[:10] + "…", self._cooldown[_b_low] - _now_exec)
            opp_list.append((opp, _b_low, should_exec))

        # ── Fase 2: bundle attempt + fallback individual ──────────────────────
        tx_map: dict[str, str | None] = {}
        bundled: set[str] = set()
        to_exec = sorted(
            [(opp, bl) for opp, bl, se in opp_list if se],
            key=lambda x: -x[0].net_profit_usd,
        )
        if to_exec:
            logger.info("AavePolygon: %d liquidações prontas", len(to_exec))
            if len(to_exec) >= 2:
                bundled = self._try_bundle(to_exec)
                for _bl in bundled:
                    tx_map[_bl] = "bundle"

            remaining = [(opp, bl) for opp, bl in to_exec if bl not in bundled]
            if remaining:
                def _exec_task(pair):
                    opp, bl = pair
                    try:
                        with self._wallet_pool.borrow(timeout=1.5) as _pk:
                            return bl, self._execute_live(opp, pk=_pk)
                    except Exception as _e:
                        logger.warning("AavePolygon: wallet indisponível para %s: %s",
                                       bl[:10] + "…", _e)
                        return bl, None

                with ThreadPoolExecutor(max_workers=self._wallet_pool.size) as _ex:
                    for _bl, _tx in _ex.map(_exec_task, remaining):
                        tx_map[_bl] = _tx

        # ── Fase 3: resultados + state update ────────────────────────────────
        results: list[dict] = []
        for opp, _b_low, should_exec in opp_list:
            tx_hash = tx_map.get(_b_low) if should_exec else None
            executed = tx_hash is not None
            if should_exec:
                if tx_hash:
                    self._fail_counts.pop(_b_low, None)
                    self.notifier.notify(
                        "trade_executed",
                        f"🟣 LIQUIDAÇÃO Polygon executada {opp.borrower[:10]}… "
                        f"lucro≈${opp.net_profit_usd:.2f} | tx={tx_hash[:20]}…",
                    )
                else:
                    _cnt = self._fail_counts.get(_b_low, 0) + 1
                    self._fail_counts[_b_low] = _cnt
                    if _cnt >= _BLACKLIST_FAILS:
                        self._blacklist[_b_low] = opp.health_factor
                        logger.warning("AavePolygon: %s adicionado à blacklist (%d falhas)",
                                       opp.borrower[:10] + "…", _cnt)
            self._record(opp, executed=executed, tx_hash=tx_hash)
            results.append({
                "borrower":   opp.borrower,
                "hf":         opp.health_factor,
                "debt_usd":   opp.total_debt_usd,
                "profit_usd": opp.net_profit_usd,
                "executed":   executed,
                "dry_run":    self.dry_run,
            })

        if results:
            logger.info("AavePolygon: %d oportunidades (%d candidatos)", len(results), len(candidates))
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
    print("AAVE V3 POLYGON LIQUIDATOR — Smoke Test")
    print("=" * 60)

    bot = AaveLiquidatorPolygonBot()

    connected = bot._connected()
    print(f"\n[1] RPC Polygon conectado: {connected}")
    if not connected:
        print("    AVISO: sem conectividade — verificar ALCHEMY_POLYGON_URL no .env")
        sys.exit(0)

    try:
        bloco = bot.w3.eth.block_number
        print(f"[2] Bloco actual Polygon: {bloco:,}")
    except Exception as exc:
        print(f"[2] Bloco: ERRO — {exc}")
        sys.exit(1)

    reserves = bot._reserves_list()
    print(f"[3] Reserves Aave V3 Polygon: {len(reserves)} activos")
    for r in reserves[:5]:
        print(f"    {r}")
    if len(reserves) > 5:
        print(f"    … (+{len(reserves) - 5} mais)")

    matic_px = bot._matic_price()
    print(f"[4] MATIC price (oracle Aave): ${matic_px:,.4f}")

    orig = bot.scan_blocks
    bot.scan_blocks = 500
    print("\n[5] A procurar mutuários nos últimos 500 blocos…")
    bot._scan_borrowers()
    print(f"    Mutuários encontrados: {len(bot._borrowers)}")
    bot.scan_blocks = orig

    print(f"\n[6] A verificar health factors (threshold={bot.hf_threshold})…")
    found = 0
    for addr in list(bot._borrowers)[:20]:
        data = bot._check_health(addr)
        if data is None:
            continue
        hf, col, debt = data
        opp = bot._estimate(addr, hf, col, debt)
        status = "⚠️  LIQUIDÁVEL" if hf < bot.hf_threshold else "✅ seguro"
        print(f"    {addr[:14]}…  HF={hf:.4f}  dívida=${debt:.2f}  {status}")
        if hf < bot.hf_threshold:
            print(f"       lucro≈${opp.net_profit_usd:.4f}  bonus={opp.liquidation_bonus_pct:.1f}%"
                  f"  gas≈${opp.gas_cost_usd:.6f}")
            found += 1

    print(f"\n    Total liquidáveis: {found} (dry_run={bot.dry_run})")

    from utils.database import get_session
    from sqlalchemy import text
    with get_session() as s:
        n = s.execute(text("SELECT COUNT(*) FROM liquidation_opportunities")).scalar()
    print(f"\n[7] BD liquidation_opportunities: {n} registos")

    print("\nSMOKE OK")
