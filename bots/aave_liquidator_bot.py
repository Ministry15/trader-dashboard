"""Bot de liquidações Aave V3 na chain Base.

Estratégia:
  1. Mantém lista de mutuários via scanning de eventos Borrow
  2. Verifica health factor de cada posição a cada poll_seconds
  3. Posições com HF < health_factor_threshold (default: 1.05):
       – Calcula dívida a cobrir (50% do total, limite Aave)
       – Estima colateral a receber (dívida × (1 + bonus))
       – Estima custo de gas (Base L2 muito barato)
       – Calcula lucro líquido em USD
  4. DRY_RUN=true: regista oportunidade na BD, NÃO executa
  5. DRY_RUN=false: chama contrato de flash loan para executar

Contratos Aave V3 Base (mainnet, chain 8453):
  Pool:                0xA238Dd80C259a72e81d7e4664a9801593F98d1c5
  PoolAddressProvider: 0xe20fCBdBfFC4Dd138cE8b2E6FBb6CB49777ad64D
  PriceOracle:         0x2Cc0Fc26eD4563A5ce5e8bdcfe1a2878676Ae156

Contrato de liquidação (flash loan deployado):
  0x9531F6F28202B7E83717b31992035F769046135B

RPC: ALCHEMY_BASE_URL do .env (fallback: https://mainnet.base.org)
"""
from __future__ import annotations

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

# ── Aave V3 Base — endereços ────────────────────────────────────────────────

POOL_ADDRESS       = Web3.to_checksum_address("0xA238Dd80C259a72e81d7e4664a9801593F98d1c5")
ORACLE_ADDRESS     = Web3.to_checksum_address("0x2Cc0Fc26eD4563A5ce5e8bdcfe1a2878676Ae156")
FLASH_LIQ_ADDRESS      = Web3.to_checksum_address("0x843730A2114b8624a36B4D4956aDdc6005bc5c30")
DATA_PROVIDER_ADDRESS  = Web3.to_checksum_address("0x0F43731EB8d45A581f4a36DD74F5f358bc90C73A")
BASE_CHAIN_ID          = 8453
WETH_BASE          = "0x4200000000000000000000000000000000000006"

# Bonus de liquidação confirmados (Aave V3 Base governance proposal)
_BONUS: dict[str, float] = {
    "0x4200000000000000000000000000000000000006": 0.050,  # WETH  → 5%
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": 0.050,  # USDC  → 5%
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca": 0.050,  # USDbC → 5%
    "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf": 0.075,  # cbBTC → 7.5%
    "0x2ae3f1ec7f1f5012cfeab0185bfc7aa3cf0dec22": 0.075,  # cbETH → 7.5%
    "0xc1cba3fcea344f92d9239c08c0568f6f2f0ee452": 0.070,  # wstETH→ 7%
}
_DEFAULT_BONUS     = 0.050  # 5% conservador

_HF_LIQUIDATABLE      = 1.0   # Aave V3: só liquidável quando HF < 1.0 (monitorização em 1.2)
_HF_RESERVE_THRESHOLD = 1.05  # reserve_data + _estimate só para HF abaixo disto; acima: só monitorizar

_FLASHBOTS_ENDPOINT      = "https://relay.flashbots.net"
_FLASHBOTS_MIN_PROFIT_USD = 500.0
_DEBT_MIN_USD    = 500.0
_MAX_BUNDLE_TXS  = 4
_DEBT_MAX_USD    = 50_000.0
_BLACKLIST_FAILS = 3

_TOKEN_SYMBOLS: dict[str, str] = {
    "0x4200000000000000000000000000000000000006": "WETH",
    "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf": "cbBTC",
    "0x2ae3f1ec7f1f5012cfeab0185bfc7aa3cf0dec22": "cbETH",
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": "USDC",
    "0x50c5725949a6f0c72e6c4a641f24049a917db0cb": "DAI",
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca": "USDbC",
}

_GAS_UNITS         = 500_000   # estimativa para flash loan + liquidação
_ORACLE_DECIMALS   = 8         # Aave oracle: USD com 8 decimais
_HF_DECIMALS       = 18        # healthFactor em ray (1e18)
_ACCOUNT_DECIMALS  = 8         # totalCollateralBase/totalDebtBase em USD×1e8

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
            {"indexed": True,  "name": "collateralAsset",           "type": "address"},
            {"indexed": True,  "name": "debtAsset",                 "type": "address"},
            {"indexed": True,  "name": "user",                      "type": "address"},
            {"indexed": False, "name": "debtToCover",               "type": "uint256"},
            {"indexed": False, "name": "liquidatedCollateralAmount", "type": "uint256"},
            {"indexed": False, "name": "liquidator",                "type": "address"},
            {"indexed": False, "name": "receiveAToken",             "type": "bool"},
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
            {"name": "debtAsset",      "type": "address"},
            {"name": "collateralAsset","type": "address"},
            {"name": "borrower",       "type": "address"},
            {"name": "debtAmount",     "type": "uint256"},
            {"name": "poolFee",        "type": "uint24"},
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
class LiqOpportunity:
    borrower: str
    health_factor: float
    total_collateral_usd: float
    total_debt_usd: float
    debt_to_cover_usd: float
    collateral_to_receive_usd: float
    liquidation_bonus_pct: float
    gas_cost_usd: float
    net_profit_usd: float
    debt_asset: str   = field(default="")
    collateral_asset: str = field(default="")


# ── Bot ───────────────────────────────────────────────────────────────────────

class AaveLiquidatorBot:
    """Monitoriza e (em modo live) executa liquidações Aave V3 na Base chain."""

    def __init__(self, settings: dict | None = None):
        self.settings = settings or get_settings()
        self.cfg = self.settings.get("bots", {}).get("aave_liquidator", {})

        _base_rpc_pool = [
            "https://base-rpc.publicnode.com",
            "https://1rpc.io/base",
            "https://mainnet.base.org",
        ]
        primary_rpc = get_env("ALCHEMY_BASE_URL") or _base_rpc_pool[0]
        self._rpc_urls: list[str] = list(dict.fromkeys([primary_rpc] + _base_rpc_pool))
        self._active_rpc: str = primary_rpc

        self.dry_run     : bool  = False
        self.hf_threshold: float = float(self.cfg.get("health_factor_threshold", 1.0))
        self.min_profit  : float = float(self.cfg.get("min_profit_usd", 8.0))
        self.scan_blocks : int   = int(self.cfg.get("borrower_scan_blocks", 50_000))
        self.max_per_tick: int   = int(self.cfg.get("max_positions_per_tick", 50))

        self.w3 = Web3(Web3.HTTPProvider(primary_rpc, request_kwargs={"timeout": 30}))

        self.pool    = self.w3.eth.contract(address=POOL_ADDRESS,      abi=_POOL_ABI)
        self.oracle  = self.w3.eth.contract(address=ORACLE_ADDRESS,    abi=_ORACLE_ABI)
        self.flash         = self.w3.eth.contract(address=FLASH_LIQ_ADDRESS,     abi=_FLASH_LIQ_ABI)
        self.multicall     = self.w3.eth.contract(address=MULTICALL3_ADDRESS,    abi=_MULTICALL3_ABI)
        self.data_provider = self.w3.eth.contract(address=DATA_PROVIDER_ADDRESS, abi=_POOL_ABI)

        self._borrowers: set[str] = set()
        self._scan_from: int = 0
        self._reserves : list[str] = []
        self._eth_price_cache: float = 2000.0
        self._eth_price_ts   : float = 0.0
        self._decimals_cache : dict[str, int] = {}
        self._price_cache    : dict[str, int] = {}
        self._cooldown       : dict[str, float] = {}
        self._fail_counts    : dict[str, int]   = {}
        self._blacklist      : dict[str, float] = {}

        self.notifier = TelegramNotifier(self.settings)
        init_db()

        logger.info(
            "AaveLiquidatorBot: rpc=%s dry_run=%s hf<%.2f min_profit=$%.2f",
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
                self.w3 = test_w3
                self._active_rpc = url
                self.pool  = self.w3.eth.contract(address=POOL_ADDRESS,   abi=_POOL_ABI)
                self.oracle= self.w3.eth.contract(address=ORACLE_ADDRESS,  abi=_ORACLE_ABI)
                self.flash         = self.w3.eth.contract(address=FLASH_LIQ_ADDRESS,     abi=_FLASH_LIQ_ABI)
                self.multicall     = self.w3.eth.contract(address=MULTICALL3_ADDRESS,    abi=_MULTICALL3_ABI)
                self.data_provider = self.w3.eth.contract(address=DATA_PROVIDER_ADDRESS, abi=_POOL_ABI)
                logger.warning("RPC trocado para fallback: %s", url.split("//")[-1].split("/")[0])
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
                            "RPC rate-limit (429) — a tentar fallback (tentativa %d/3)",
                            attempt + 1,
                        )
                        if self._switch_rpc(self._active_rpc):
                            continue
                    wait = 2 ** attempt
                    logger.debug("429 rate-limit, aguardar %ds…", wait)
                    time.sleep(wait)
                else:
                    # 403 Forbidden, 5xx, etc. — trocar RPC imediatamente
                    logger.warning("HTTP %s no RPC Base — a tentar fallback", status)
                    if attempt == 0 and self._switch_rpc(self._active_rpc):
                        continue
                    return False
            except Exception as exc:
                logger.debug("Erro ao verificar ligação RPC: %s", exc)
                if attempt == 0 and self._switch_rpc(self._active_rpc):
                    continue
                return False
        return False

    def _eth_price(self) -> float:
        now = time.time()
        if now - self._eth_price_ts < 300:
            return self._eth_price_cache
        try:
            raw = self.oracle.functions.getAssetPrice(
                Web3.to_checksum_address(WETH_BASE)).call()
            self._eth_price_cache = raw / 10 ** _ORACLE_DECIMALS
            self._eth_price_ts    = now
        except Exception as exc:
            logger.debug("Oracle ETH price falhou: %s — cache $%.2f", exc, self._eth_price_cache)
        return self._eth_price_cache

    def _gas_price_gwei(self) -> float:
        try:
            return self.w3.eth.gas_price / 1e9
        except Exception:
            return 0.005   # Base L2: ~0.005 Gwei típico

    def _calc_gas_price(self, net_profit_usd: float) -> int:
        """Priority tip em 3 tiers baseado no lucro estimado. Devolve gasPrice em Wei."""
        try:
            base_fee = self.w3.eth.get_block("latest")["baseFeePerGas"]
        except Exception:
            base_fee = int(0.005 * 1e9)  # 0.005 gwei fallback (Base L2)
        if net_profit_usd < 50:
            tip_gwei = 1.5
        elif net_profit_usd < 500:
            tip_gwei = 7.5
        else:
            tip_gwei = 25.0
        gas_price = base_fee + int(tip_gwei * 1e9)
        logger.info(
            "Base: gas_price=%.4f gwei (base=%.4f + tip=%.1f) lucro≈$%.2f",
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
                logger.warning("Multicall3 account_data chunk [%d] falhou: %s", i, exc)
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
        logger.info("Multicall3 account_data: %d borrowers → %d com dívida",
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
                logger.warning("Multicall3 reserve_data chunk [%d] falhou: %s", i, exc)
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
                    "best_debt_val": 0, "debt_asset": WETH_BASE,
                    "best_col_val":  0, "col_asset":  WETH_BASE,
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
        logger.info("Multicall3 reserve_data: %d×%d=%d calls em %d chunk(s) → %d resultados",
                    len(borrowers), len(reserves), len(calls), n_chunks, len(result))
        return result

    def _reserves_list(self) -> list[str]:
        if not self._reserves:
            try:
                self._reserves = [
                    Web3.to_checksum_address(r)
                    for r in self.pool.functions.getReservesList().call()
                ]
                logger.info("Reserves Aave Base: %d activos", len(self._reserves))
            except Exception as exc:
                logger.warning("getReservesList falhou: %s", exc)
        return self._reserves

    # ── descoberta de mutuários ───────────────────────────────────────────────

    def _scan_borrowers(self) -> None:
        """Varre eventos Borrow para encontrar mutuários activos."""
        if not self._connected():
            return
        try:
            latest = self.w3.eth.block_number
        except Exception as exc:
            logger.warning("Não consegui ler bloco actual: %s", exc)
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
                logger.debug("Borrow events [%d..%d]: %s", from_block, to_block, exc)
                break   # RPC limit — tentar na próxima tick

        self._scan_from = latest

        # Remover posições já liquidadas
        try:
            liq_start = max(0, latest - 5_000)
            liq_evts  = self.pool.events.LiquidationCall().get_logs(
                from_block=liq_start, to_block=latest)
            for e in liq_evts:
                self._borrowers.discard(e.args["user"].lower())
        except Exception:
            pass

        if added:
            logger.info("Scan Borrow: +%d mutuários (total=%d, bloco=%d)",
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
            logger.debug("getUserAccountData(%s…): %s", address[:10], exc)
            return None

    def _estimate(self, borrower: str, hf: float,
                  col_usd: float, debt_usd: float,
                  _pre: dict | None = None) -> LiqOpportunity:
        if _pre is not None:
            entry            = _pre.get(borrower.lower(), {})
            debt_asset       = entry.get("debt_asset", WETH_BASE)
            collateral_asset = entry.get("col_asset",  WETH_BASE)
        else:
            # fallback sequencial (Multicall3 falhou totalmente)
            reserves = self._reserves_list()
            debt_asset = collateral_asset = WETH_BASE
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

        bonus            = _BONUS.get(collateral_asset.lower(), _DEFAULT_BONUS)
        debt_to_cover    = debt_usd * 0.50   # Aave: máx 50% por liquidação
        col_to_receive   = debt_to_cover * (1.0 + bonus)
        gas_usd          = _GAS_UNITS * self._gas_price_gwei() * 1e-9 * self._eth_price()
        net_profit       = col_to_receive - debt_to_cover - gas_usd

        return LiqOpportunity(
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

    def _record(self, opp: LiqOpportunity, executed: bool,
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
        )
        action = "INSERT" if inserted else "UPDATE"
        logger.debug("BD %s id=%d %s HF=%.4f", action, rec_id, opp.borrower[:10], opp.health_factor)

    def _execute_live(self, opp: LiqOpportunity, nonce: int) -> str | None:
        """Executa flash loan liquidation via contrato deployado."""
        try:
            pk   = get_env("BSC_PRIVATE_KEY") or ""
            acct = self.w3.eth.account.from_key(pk)
            # Converter USD → unidades de token (aproximação via oracle)
            debt_oracle  = self._get_asset_price(opp.debt_asset)
            token_dec    = self._token_decimals(opp.debt_asset)
            debt_units   = int(opp.debt_to_cover_usd
                               / (debt_oracle / 10 ** _ORACLE_DECIMALS)
                               * 10 ** token_dec)

            _POOL_FEES = {
                "0x4200000000000000000000000000000000000006": 500,  # WETH
                "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf": 500,  # cbBTC
                "0x2ae3f1ec7f1f5012cfeab0185bfc7aa3cf0dec22": 500,  # cbETH
                "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": 100,  # USDC
            }
            pool_fee = _POOL_FEES.get(opp.collateral_asset.lower(), 3000)

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
                    "Base: simulação falhou %s — cooldown 10min: %s",
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
                "chainId":  BASE_CHAIN_ID,
                "gas":      _GAS_UNITS,
                "gasPrice": self._calc_gas_price(opp.net_profit_usd),
                "nonce":    nonce,
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
                        logger.info("AaveBase: TX via Flashbots @ bloco %d: %s…", _tgt, _exp.hex()[:18])
                        return _exp.hex()
                except Exception as _fb_exc:
                    logger.warning("AaveBase: Flashbots falhou — fallback mempool: %s", _fb_exc)
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            logger.info("LIQUIDAÇÃO TX: %s", tx_hash.hex())
            return tx_hash.hex()
        except Exception as exc:
            if "HEALTH_FACTOR_NOT_BELOW_THRESHOLD" in str(exc):
                until = time.time() + 600
                self._cooldown[opp.borrower.lower()] = until
                logger.warning(
                    "Base: %s HF acima do threshold — cooldown 10min",
                    opp.borrower[:10] + "…",
                )
            else:
                logger.error("Falha ao executar liquidação: %s", exc)
            return None

    # ── Multi-bundle (Phase 6) ───────────────────────────────────────────────
    def _try_bundle(self, opps_and_keys: list[tuple], base_nonce: int) -> set[str]:
        """Bundle N Aave executeFlashLiquidation calls into one Flashbots bundle.
        opps_and_keys: list of (opp, b_low). Returns set of b_low that were bundled."""
        if self.flash is None or len(opps_and_keys) < 2:
            return set()
        try:
            pk   = get_env("BSC_PRIVATE_KEY") or ""
            acct = self.w3.eth.account.from_key(pk)
            gas  = self._calc_gas_price(max(o.net_profit_usd for o, _ in opps_and_keys))
            _POOL_FEES_BUNDLE = {
                "0x4200000000000000000000000000000000000006": 500,
                "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf": 500,
                "0x2ae3f1ec7f1f5012cfeab0185bfc7aa3cf0dec22": 500,
                "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": 100,
            }
            raw_txes, keys = [], []
            for i, (opp, b_low) in enumerate(opps_and_keys[:_MAX_BUNDLE_TXS]):
                debt_oracle = self._get_asset_price(opp.debt_asset)
                token_dec   = self._token_decimals(opp.debt_asset)
                debt_units  = int(opp.debt_to_cover_usd
                                  / (debt_oracle / 10 ** _ORACLE_DECIMALS)
                                  * 10 ** token_dec)
                pool_fee = _POOL_FEES_BUNDLE.get(opp.collateral_asset.lower(), 3000)
                tx = self.flash.functions.executeFlashLiquidation(
                    Web3.to_checksum_address(opp.debt_asset),
                    Web3.to_checksum_address(opp.collateral_asset),
                    Web3.to_checksum_address(opp.borrower),
                    debt_units, pool_fee,
                ).build_transaction({
                    "from":    acct.address,
                    "chainId": BASE_CHAIN_ID,
                    "gas":     _GAS_UNITS,
                    "gasPrice": gas,
                    "nonce":   base_nonce + i,
                })
                signed = acct.sign_transaction(tx)
                raw_txes.append("0x" + signed.raw_transaction.hex())
                keys.append(b_low)
            target = self.w3.eth.block_number + 1
            bh = _fb_send_multi(raw_txes, target, _FLASHBOTS_ENDPOINT, pk)
            if bh:
                logger.info("AaveBase: bundle %d txs @ bloco %d: %s…",
                            len(raw_txes), target, bh[:16])
                return set(keys)
            return set()
        except Exception as exc:
            logger.warning("AaveBase: bundle falhou → fallback individual: %s", exc)
            return set()

    # ── tick ─────────────────────────────────────────────────────────────────

    def tick(self) -> list[dict]:
        if not self._connected():
            logger.warning("AaveLiquidator: sem ligação ao RPC Base — tick saltado")
            return []

        self._scan_borrowers()
        self._price_cache = {}

        candidates = list(self._borrowers)[:self.max_per_tick]
        if not candidates:
            return []

        # Fase 1: getUserAccountData de todos os candidatos → 1 Multicall3
        account_data = self._batch_account_data(candidates)
        if not account_data:
            logger.warning("AaveLiquidator: Multicall3 account_data falhou — fallback sequencial")
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
            logger.debug("AaveLiquidator: 0 posições elegíveis (%d candidatos)", len(candidates))
            return []

        # Fase 2: reserve_data só para posições iminentes (HF < _HF_RESERVE_THRESHOLD).
        # Posições HF >= _HF_RESERVE_THRESHOLD são apenas monitorizadas — sem reserve_data
        # nem _estimate, o que elimina a fase mais lenta (~2s) nos ticks normais.
        imminent     = [(b, hf, col, debt) for b, hf, col, debt in eligible
                        if hf < _HF_RESERVE_THRESHOLD]
        monitor_only = [(b, hf, col, debt) for b, hf, col, debt in eligible
                        if hf >= _HF_RESERVE_THRESHOLD]

        if monitor_only:
            for b, hf, col, debt in monitor_only:
                logger.info(
                    "Tick: monitorar %s HF=%.4f col=$%.0f debt=$%.0f (aguarda HF<%.2f)",
                    b[:10] + "…", hf, col, debt, _HF_RESERVE_THRESHOLD,
                )

        if not imminent:
            logger.info(
                "AaveLiquidator: 0 iminentes, %d monitorizados (%d candidatos)",
                len(monitor_only), len(candidates),
            )
            return []

        reserves     = self._reserves_list()
        reserve_data = self._batch_reserve_data([b for b, *_ in imminent], reserves)

        # Nonce sequencial: 1 fetch por tick, incrementa só quando TX é aceite
        _pk    = get_env("BSC_PRIVATE_KEY") or ""
        _acct  = self.w3.eth.account.from_key(_pk)
        _nonce = self.w3.eth.get_transaction_count(_acct.address, 'pending')

        # Saldo mínimo: suspende execução se ETH insuficiente para gas
        if not self.dry_run:
            _bal_wei = self.w3.eth.get_balance(_acct.address)
            if _bal_wei < Web3.to_wei(0.005, 'ether'):
                logger.error(
                    "AaveLiquidator: saldo insuficiente (%.6f ETH < 0.005) — execução suspensa",
                    float(Web3.from_wei(_bal_wei, 'ether')),
                )
                return []

        # Sumário do tick
        _now_tick    = time.time()
        _n_liq       = sum(1 for _, hf, _, _ in imminent if hf < _HF_LIQUIDATABLE)
        _n_cooldown  = sum(1 for b, *_ in imminent if self._cooldown.get(b.lower(), 0) > _now_tick)
        _n_blacklist = len(self._blacklist)
        logger.info(
            "Tick: %d elegíveis (%d iminentes) | %d liquidáveis (HF<1.0) | %d cooldown | %d blacklist",
            len(eligible), len(imminent), _n_liq, _n_cooldown, _n_blacklist,
        )

        # ── Bundle attempt (Phase 6) ──────────────────────────────────────────
        bundled: set[str] = set()
        if not self.dry_run and _n_liq >= 2:
            _bdl_list = []
            for _b, _hf, _cu, _du in imminent:
                _bl = _b.lower()
                if _bl in self._blacklist: continue
                if _hf >= _HF_LIQUIDATABLE: continue
                if self._cooldown.get(_bl, 0) > _now_tick: continue
                _o = self._estimate(_b, _hf, _cu, _du, _pre=reserve_data if reserve_data else None)
                if _o.net_profit_usd < self.min_profit: continue
                _bdl_list.append((_o, _bl))
            if len(_bdl_list) >= 2:
                _bdl_sorted = sorted(_bdl_list, key=lambda x: -x[0].net_profit_usd)
                bundled = self._try_bundle(_bdl_sorted, _nonce)
                if bundled:
                    _nonce += len(bundled)
                    for _bk in bundled:
                        self._cooldown[_bk] = _now_tick + 300
                        self._fail_counts.pop(_bk, None)
                    logger.info("AaveBase: %d liquidações via Flashbots bundle", len(bundled))

        results: list[dict] = []
        for borrower, hf, col_usd, debt_usd in imminent:
            _b_low = borrower.lower()

            # Blacklist permanente: salta se HF não mudou > 5%
            if _b_low in self._blacklist:
                _bl_hf = self._blacklist[_b_low]
                if abs(hf - _bl_hf) / max(_bl_hf, 0.001) < 0.05:
                    logger.debug("Base: %s blacklisted (HF=%.4f base=%.4f) — saltado",
                                 borrower[:10] + "…", hf, _bl_hf)
                    continue
                del self._blacklist[_b_low]
                self._fail_counts.pop(_b_low, None)
                logger.info("Base: %s saiu da blacklist (HF %.4f→%.4f)",
                            borrower[:10] + "…", _bl_hf, hf)

            opp = self._estimate(borrower, hf, col_usd, debt_usd,
                                 _pre=reserve_data if reserve_data else None)

            _debt_sym = _TOKEN_SYMBOLS.get(opp.debt_asset.lower(), opp.debt_asset[-6:])
            _col_sym  = _TOKEN_SYMBOLS.get(opp.collateral_asset.lower(), opp.collateral_asset[-6:])
            logger.info(
                "LIQUIDAÇÃO %s HF=%.4f debt=$%.2f(%s) col=$%.2f(%s) lucro≈$%.4f dry=%s",
                borrower[:10] + "…", hf,
                debt_usd, _debt_sym,
                col_usd, _col_sym,
                opp.net_profit_usd, self.dry_run,
            )

            tx_hash, executed = None, False
            if not self.dry_run and hf < _HF_LIQUIDATABLE and opp.net_profit_usd >= self.min_profit:
                _now   = time.time()
                _until = self._cooldown.get(_b_low, 0)
                if _until > _now:
                    logger.debug(
                        "Base: %s em cooldown (%.0fs restantes) — saltado",
                        borrower[:10] + "…", _until - _now,
                    )
                elif _b_low in bundled:
                    tx_hash = "bundle"; executed = True
                else:
                    tx_hash = self._execute_live(opp, nonce=_nonce)
                    if tx_hash is None:
                        _cnt = self._fail_counts.get(_b_low, 0) + 1
                        self._fail_counts[_b_low] = _cnt
                        if _cnt >= _BLACKLIST_FAILS:
                            self._blacklist[_b_low] = hf
                            logger.warning(
                                "Base: %s adicionado à blacklist (%d falhas consecutivas)",
                                borrower[:10] + "…", _cnt,
                            )
                    else:
                        self._fail_counts.pop(_b_low, None)
                executed = tx_hash is not None
                if executed:
                    _nonce += 1
                    self.notifier.notify(
                        "trade_executed",
                        f"🔴 LIQUIDAÇÃO executada {borrower[:10]}… "
                        f"lucro≈${opp.net_profit_usd:.2f} | tx={tx_hash[:20]}…",
                    )

            self._record(opp, executed=executed, tx_hash=tx_hash)

            results.append({
                "borrower":    borrower,
                "hf":          opp.health_factor,
                "debt_usd":    opp.total_debt_usd,
                "profit_usd":  opp.net_profit_usd,
                "executed":    executed,
                "dry_run":     self.dry_run,
            })

        if results:
            logger.info("AaveLiquidator: %d oportunidades (%d candidatos)", len(results), len(candidates))
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
    print("AAVE V3 BASE LIQUIDATOR — Smoke Test")
    print("=" * 60)

    bot = AaveLiquidatorBot()

    # 1. Conectividade
    connected = bot._connected()
    print(f"\n[1] RPC Base conectado: {connected}")
    if not connected:
        print("    AVISO: sem conectividade — inicializar ALCHEMY_BASE_URL no .env")
        print("    Exemplo: ALCHEMY_BASE_URL=https://base-mainnet.g.alchemy.com/v2/<KEY>")
        sys.exit(0)

    # 2. Bloco actual
    try:
        bloco = bot.w3.eth.block_number
        print(f"[2] Bloco actual Base: {bloco:,}")
    except Exception as exc:
        print(f"[2] Bloco: ERRO — {exc}")
        sys.exit(1)

    # 3. Reserves Aave V3 Base
    reserves = bot._reserves_list()
    print(f"[3] Reserves Aave V3 Base: {len(reserves)} activos")
    for r in reserves[:5]:
        print(f"    {r}")
    if len(reserves) > 5:
        print(f"    … (+{len(reserves) - 5} mais)")

    # 4. Preço ETH via oracle
    eth_px = bot._eth_price()
    print(f"[4] ETH price (oracle Aave): ${eth_px:,.2f}")

    # 5. Scan mutuários (últimos 500 blocos — rápido)
    orig = bot.scan_blocks
    bot.scan_blocks = 500
    print(f"\n[5] A procurar mutuários nos últimos 500 blocos…")
    bot._scan_borrowers()
    print(f"    Mutuários encontrados: {len(bot._borrowers)}")
    bot.scan_blocks = orig

    # 6. Verificar posições liquidáveis
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

    # 7. Test DB
    from utils.database import get_session
    from sqlalchemy import text
    with get_session() as s:
        n = s.execute(text("SELECT COUNT(*) FROM liquidation_opportunities")).scalar()
    print(f"\n[7] BD liquidation_opportunities: {n} registos")

    print("\nSMOKE OK")
