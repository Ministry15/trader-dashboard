"""Bot de liquidações Aave V3 na chain Avalanche C-Chain.

Estratégia idêntica ao aave_liquidator_polygon_bot.py, mas independente:
  1. Mantém lista de mutuários via scanning de eventos Borrow
  2. Verifica health factor de cada posição a cada poll_seconds
  3. Posições com HF < health_factor_threshold (default: 1.0):
       – Calcula dívida a cobrir (50% do total, limite Aave)
       – Estima colateral a receber (dívida × (1 + bonus))
       – Estima custo de gas (Avalanche usa AVAX)
       – Calcula lucro líquido em USD
  4. DRY_RUN=true: regista oportunidade na BD, NÃO executa
  5. DRY_RUN=false: chama contrato de flash loan para executar
       (requer flash_loan_contract em settings.yaml aave_liquidator_avax)

Contratos Aave V3 Avalanche C-Chain (mainnet, chain 43114):
  Pool:        0x794a61358D6845594F94dc1DB02A252b5b4814aD
  PriceOracle: 0xEBd36016B3eD09D4693Ed4251c67Bd858c3c7C9C

RPC: ALCHEMY_AVAX_URL do .env (fallback: https://api.avax.network/ext/bc/C/rpc)
"""
from __future__ import annotations

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

# ── Aave V3 Avalanche — endereços ─────────────────────────────────────────────

POOL_ADDRESS    = Web3.to_checksum_address("0x794a61358D6845594F94dc1DB02A252b5b4814aD")
ORACLE_ADDRESS  = Web3.to_checksum_address("0xEBd36016B3eD09D4693Ed4251c67Bd858c3c7C9C")
AVAX_CHAIN_ID   = 43114
WAVAX_AVAX      = "0xB31f66AA3C1e785363F0875A1B74E27b85FD66c7"  # gas token para custo USD
_AVAX_FALLBACK_RPC = "https://api.avax.network/ext/bc/C/rpc"

# Bonus de liquidação Aave V3 Avalanche (valores conservadores)
_BONUS: dict[str, float] = {
    "0xb31f66aa3c1e785363f0875a1b74e27b85fd66c7": 0.100,  # WAVAX   → 10%
    "0x49d5c2bdffac6ce2bfdb6640f4f80f226bc10bab": 0.050,  # WETH.e  → 5%
    "0xb97ef9ef8734c71904d8002f8b6bc66dd9c48a6": 0.050,  # USDC    → 5%
    "0xa7d7079b0fead91f3e65f86e8915cb59c1a4c664": 0.050,  # USDC.e  → 5%
    "0xc7198437980c041c805a1edcba50c1ce5db95118": 0.050,  # USDT.e  → 5%
    "0xd586e7f844cea2f87f50152665bcbc2c279d8d70": 0.050,  # DAI.e   → 5%
    "0x152b9d0fdc40c096757f570a51e494bd4b943e50": 0.075,  # BTC.b   → 7.5%
    "0x2b2c81e08f1af8835a78bb2a90ae924ace0ea4be": 0.100,  # sAVAX   → 10%
}
_DEFAULT_BONUS    = 0.050  # 5% conservador

_HF_LIQUIDATABLE = 1.0      # Aave V3: só liquidável quando HF < 1.0
_DEBT_MIN_USD    = 500.0
_DEBT_MAX_USD    = 50_000.0
_BLACKLIST_FAILS = 3

_TOKEN_SYMBOLS: dict[str, str] = {
    "0xb31f66aa3c1e785363f0875a1b74e27b85fd66c7": "WAVAX",
    "0x49d5c2bdffac6ce2bfdb6640f4f80f226bc10bab": "WETH.e",
    "0xb97ef9ef8734c71904d8002f8b6bc66dd9c48a6":  "USDC",
    "0xa7d7079b0fead91f3e65f86e8915cb59c1a4c664":  "USDC.e",
    "0xc7198437980c041c805a1edcba50c1ce5db95118":  "USDT.e",
    "0xd586e7f844cea2f87f50152665bcbc2c279d8d70":  "DAI.e",
    "0x152b9d0fdc40c096757f570a51e494bd4b943e50":  "BTC.b",
    "0x2b2c81e08f1af8835a78bb2a90ae924ace0ea4be":  "sAVAX",
}

_GAS_UNITS        = 500_000   # estimativa flash loan + liquidação
_ORACLE_DECIMALS  = 8         # Aave oracle: USD com 8 decimais
_HF_DECIMALS      = 18        # healthFactor em ray (1e18)
_ACCOUNT_DECIMALS = 8         # totalCollateralBase/totalDebtBase em USD×1e8

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

_FLASH_LIQ_ABI = [
    {
        "inputs": [
            {"name": "debtAsset",       "type": "address"},
            {"name": "collateralAsset", "type": "address"},
            {"name": "borrower",        "type": "address"},
            {"name": "debtAmount",      "type": "uint256"},
        ],
        "name": "executeFlashLiquidation",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]


@dataclass
class LiqOpportunityAvax:
    borrower: str
    health_factor: float
    total_collateral_usd: float
    total_debt_usd: float
    debt_to_cover_usd: float
    collateral_to_receive_usd: float
    liquidation_bonus_pct: float
    gas_cost_usd: float
    net_profit_usd: float
    debt_asset: str       = field(default="")
    collateral_asset: str = field(default="")


# ── Bot ───────────────────────────────────────────────────────────────────────

class AaveLiquidatorAvaxBot:
    """Monitoriza e (em modo live) executa liquidações Aave V3 na Avalanche C-Chain."""

    def __init__(self, settings: dict | None = None):
        self.settings = settings or get_settings()
        self.cfg = self.settings.get("bots", {}).get("aave_liquidator_avax", {})

        primary_rpc = get_env("ALCHEMY_AVAX_URL") or _AVAX_FALLBACK_RPC
        self._rpc_urls: list[str] = [primary_rpc, _AVAX_FALLBACK_RPC]
        self._active_rpc: str = primary_rpc

        self.dry_run      : bool  = False
        self.hf_threshold : float = float(self.cfg.get("health_factor_threshold", 1.0))
        self.min_profit   : float = float(self.cfg.get("min_profit_usd", 10.0))
        self.scan_blocks  : int   = int(self.cfg.get("borrower_scan_blocks", 50_000))
        self.max_per_tick : int   = int(self.cfg.get("max_positions_per_tick", 50))

        flash_addr = self.cfg.get("flash_loan_contract", "")

        self.w3     = Web3(Web3.HTTPProvider(primary_rpc, request_kwargs={"timeout": 30}))
        self.pool   = self.w3.eth.contract(address=POOL_ADDRESS,   abi=_POOL_ABI)
        self.oracle = self.w3.eth.contract(address=ORACLE_ADDRESS, abi=_ORACLE_ABI)
        self.flash  = (
            self.w3.eth.contract(
                address=Web3.to_checksum_address(flash_addr), abi=_FLASH_LIQ_ABI
            ) if flash_addr else None
        )

        self._borrowers: set[str] = set()
        self._scan_from: int = 0
        self._reserves : list[str] = []
        self._avax_price_cache: float = 25.0
        self._avax_price_ts   : float = 0.0

        self._cooldown   : dict[str, float] = {}
        self._fail_counts: dict[str, int]   = {}
        self._blacklist  : dict[str, float] = {}

        self.notifier = TelegramNotifier(self.settings)
        init_db()

        if not flash_addr:
            logger.warning(
                "AaveAvax: flash_loan_contract não configurado — modo DRY_RUN forçado para live"
            )

        logger.info(
            "AaveAvax: rpc=%s dry_run=%s hf<%.2f min_profit=$%.2f",
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
                logger.warning("AaveAvax: RPC trocado para fallback: %s",
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
                            "AaveAvax: rate-limit (429) — a tentar fallback RPC (tentativa %d/3)",
                            attempt + 1,
                        )
                        if self._switch_rpc(self._active_rpc):
                            continue
                    wait = 2 ** attempt
                    logger.debug("AaveAvax: 429 rate-limit, aguardar %ds…", wait)
                    time.sleep(wait)
                else:
                    logger.debug("AaveAvax: HTTP %s ao verificar ligação: %s", status, exc)
                    return False
            except Exception as exc:
                logger.debug("AaveAvax: erro ao verificar ligação RPC: %s", exc)
                if attempt == 0 and self._active_rpc != _AVAX_FALLBACK_RPC:
                    if self._switch_rpc(self._active_rpc):
                        continue
                return False
        return False

    def _avax_price(self) -> float:
        now = time.time()
        if now - self._avax_price_ts < 300:
            return self._avax_price_cache
        try:
            raw = self.oracle.functions.getAssetPrice(
                Web3.to_checksum_address(WAVAX_AVAX)).call()
            self._avax_price_cache = raw / 10 ** _ORACLE_DECIMALS
            self._avax_price_ts    = now
        except Exception as exc:
            logger.debug("AaveAvax: oracle AVAX price falhou: %s — cache $%.4f",
                         exc, self._avax_price_cache)
        return self._avax_price_cache

    def _gas_price_gwei(self) -> float:
        try:
            return self.w3.eth.gas_price / 1e9
        except Exception:
            return 25.0   # Avalanche C-Chain: ~25 Gwei típico

    def _reserves_list(self) -> list[str]:
        if not self._reserves:
            try:
                self._reserves = [
                    Web3.to_checksum_address(r)
                    for r in self.pool.functions.getReservesList().call()
                ]
                logger.info("AaveAvax: Reserves Aave Avalanche: %d activos", len(self._reserves))
            except Exception as exc:
                logger.warning("AaveAvax: getReservesList falhou: %s", exc)
        return self._reserves

    # ── descoberta de mutuários ───────────────────────────────────────────────

    def _scan_borrowers(self) -> None:
        """Varre eventos Borrow para encontrar mutuários activos."""
        if not self._connected():
            return
        try:
            latest = self.w3.eth.block_number
        except Exception as exc:
            logger.warning("AaveAvax: não consegui ler bloco actual: %s", exc)
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
                logger.debug("AaveAvax: Borrow events [%d..%d]: %s", from_block, to_block, exc)
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
            logger.info("AaveAvax: Scan Borrow: +%d mutuários (total=%d, bloco=%d)",
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
            logger.debug("AaveAvax: getUserAccountData(%s…): %s", address[:10], exc)
            return None

    def _estimate(self, borrower: str, hf: float,
                  col_usd: float, debt_usd: float) -> LiqOpportunityAvax:
        reserves = self._reserves_list()
        debt_asset       = reserves[0] if reserves else WAVAX_AVAX
        collateral_asset = reserves[1] if len(reserves) > 1 else reserves[0] if reserves else WAVAX_AVAX

        bonus          = _BONUS.get(debt_asset.lower(), _DEFAULT_BONUS)
        debt_to_cover  = debt_usd * 0.50
        col_to_receive = debt_to_cover * (1.0 + bonus)
        gas_usd        = _GAS_UNITS * self._gas_price_gwei() * 1e-9 * self._avax_price()
        net_profit     = col_to_receive - debt_to_cover - gas_usd

        return LiqOpportunityAvax(
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

    def _record(self, opp: LiqOpportunityAvax, executed: bool,
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
            chain="avax",
        )
        action = "INSERT" if inserted else "UPDATE"
        logger.debug("AaveAvax: BD %s id=%d %s HF=%.4f",
                     action, rec_id, opp.borrower[:10], opp.health_factor)

    def _execute_live(self, opp: LiqOpportunityAvax, nonce: int) -> str | None:
        """Executa flash loan liquidation via contrato deployado."""
        if self.flash is None:
            logger.error(
                "AaveAvax: flash_loan_contract não configurado — não é possível executar live"
            )
            return None
        try:
            pk   = get_env("BSC_PRIVATE_KEY") or ""
            acct = self.w3.eth.account.from_key(pk)
            debt_oracle = self.oracle.functions.getAssetPrice(
                Web3.to_checksum_address(opp.debt_asset)).call()
            debt_units  = int(opp.debt_to_cover_usd * 10 ** _ORACLE_DECIMALS
                              / (debt_oracle / 10 ** _ORACLE_DECIMALS))

            # Simulação obrigatória: eth_call antes de enviar TX (zero gas se falhar)
            try:
                self.flash.functions.executeFlashLiquidation(
                    Web3.to_checksum_address(opp.debt_asset),
                    Web3.to_checksum_address(opp.collateral_asset),
                    Web3.to_checksum_address(opp.borrower),
                    debt_units,
                ).call({"from": acct.address})
            except Exception as sim_exc:
                self._cooldown[opp.borrower.lower()] = time.time() + 120
                logger.warning(
                    "AaveAvax: simulação falhou %s — cooldown 2min: %s",
                    opp.borrower[:10] + "…", sim_exc,
                )
                return None

            tx = self.flash.functions.executeFlashLiquidation(
                Web3.to_checksum_address(opp.debt_asset),
                Web3.to_checksum_address(opp.collateral_asset),
                Web3.to_checksum_address(opp.borrower),
                debt_units,
            ).build_transaction({
                "from":     acct.address,
                "chainId":  AVAX_CHAIN_ID,
                "gas":      _GAS_UNITS,
                "gasPrice": self.w3.eth.gas_price,
                "nonce":    nonce,
            })
            signed  = acct.sign_transaction(tx)
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            logger.info("AaveAvax: LIQUIDAÇÃO TX: %s", tx_hash.hex())
            return tx_hash.hex()
        except Exception as exc:
            if "HEALTH_FACTOR_NOT_BELOW_THRESHOLD" in str(exc):
                self._cooldown[opp.borrower.lower()] = time.time() + 300
                logger.warning(
                    "AaveAvax: %s HF acima do threshold — cooldown 5min",
                    opp.borrower[:10] + "…",
                )
            else:
                logger.error("AaveAvax: falha ao executar liquidação: %s", exc)
            return None

    # ── tick ─────────────────────────────────────────────────────────────────

    def tick(self) -> list[dict]:
        if not self._connected():
            logger.warning("AaveAvax: sem ligação ao RPC Avalanche — tick saltado")
            return []

        self._scan_borrowers()

        # Phase 1: construir lista elegível
        eligible: list[tuple[str, float, float, float]] = []
        checked = 0
        for borrower in list(self._borrowers):
            if checked >= self.max_per_tick:
                break
            checked += 1
            data = self._check_health(borrower)
            if data is None:
                continue
            hf, col_usd, debt_usd = data
            if hf >= self.hf_threshold:
                continue
            eligible.append((borrower, hf, col_usd, debt_usd))

        # filtro de tamanho de dívida
        eligible = [(b, hf, col, debt) for b, hf, col, debt in eligible
                    if _DEBT_MIN_USD <= debt <= _DEBT_MAX_USD]

        _nonce = 0
        if not self.dry_run:
            _acct = self.w3.eth.account.from_key(get_env("BSC_PRIVATE_KEY") or "")
            _bal_wei = self.w3.eth.get_balance(_acct.address)
            if _bal_wei < Web3.to_wei(0.05, 'ether'):
                logger.error(
                    "AaveAvax: saldo insuficiente (%.6f AVAX < 0.05) — tick saltado",
                    _bal_wei / 1e18,
                )
                return []
            _nonce = self.w3.eth.get_transaction_count(_acct.address, "pending")

        _now_tick    = time.time()
        _n_liq       = sum(1 for _, hf, _, _ in eligible if hf < _HF_LIQUIDATABLE)
        _n_cooldown  = sum(1 for b, *_ in eligible if self._cooldown.get(b.lower(), 0) > _now_tick)
        _n_blacklist = len(self._blacklist)
        logger.info(
            "AaveAvax Tick: %d elegíveis | %d liquidáveis (HF<1.0) | %d cooldown | %d blacklist",
            len(eligible), _n_liq, _n_cooldown, _n_blacklist,
        )

        results: list[dict] = []
        for borrower, hf, col_usd, debt_usd in eligible:
            _b_low = borrower.lower()

            if _b_low in self._blacklist:
                _bl_hf = self._blacklist[_b_low]
                if abs(hf - _bl_hf) / max(_bl_hf, 0.001) < 0.05:
                    logger.debug("AaveAvax: %s blacklisted (HF=%.4f base=%.4f) — saltado",
                                 borrower[:10] + "…", hf, _bl_hf)
                    continue
                del self._blacklist[_b_low]
                self._fail_counts.pop(_b_low, None)
                logger.info("AaveAvax: %s saiu da blacklist (HF %.4f→%.4f)",
                            borrower[:10] + "…", _bl_hf, hf)

            opp = self._estimate(borrower, hf, col_usd, debt_usd)
            _debt_sym = _TOKEN_SYMBOLS.get(opp.debt_asset.lower(), opp.debt_asset[-6:])
            _col_sym  = _TOKEN_SYMBOLS.get(opp.collateral_asset.lower(), opp.collateral_asset[-6:])
            logger.info(
                "AaveAvax LIQUIDAÇÃO %s HF=%.4f debt=$%.2f(%s) col=$%.2f(%s) lucro≈$%.4f dry=%s",
                borrower[:10] + "…", hf, debt_usd, _debt_sym, col_usd, _col_sym,
                opp.net_profit_usd, self.dry_run,
            )

            tx_hash, executed = None, False
            if not self.dry_run and hf < _HF_LIQUIDATABLE and opp.net_profit_usd >= self.min_profit:
                _now   = time.time()
                _until = self._cooldown.get(_b_low, 0)
                if _until > _now:
                    logger.debug("AaveAvax: %s em cooldown (%.0fs restantes) — saltado",
                                 borrower[:10] + "…", _until - _now)
                else:
                    tx_hash = self._execute_live(opp, nonce=_nonce)
                    if tx_hash is None:
                        _cnt = self._fail_counts.get(_b_low, 0) + 1
                        self._fail_counts[_b_low] = _cnt
                        if _cnt >= _BLACKLIST_FAILS:
                            self._blacklist[_b_low] = hf
                            logger.warning(
                                "AaveAvax: %s adicionado à blacklist (%d falhas consecutivas)",
                                borrower[:10] + "…", _cnt,
                            )
                    else:
                        self._fail_counts.pop(_b_low, None)
                executed = tx_hash is not None
                if executed:
                    _nonce += 1
                    self.notifier.notify(
                        "trade_executed",
                        f"🔵 LIQUIDAÇÃO Avalanche executada {borrower[:10]}… "
                        f"lucro≈${opp.net_profit_usd:.2f} | tx={tx_hash[:20]}…",
                    )

            self._record(opp, executed=executed, tx_hash=tx_hash)

            results.append({
                "borrower":   borrower,
                "hf":         opp.health_factor,
                "debt_usd":   opp.total_debt_usd,
                "profit_usd": opp.net_profit_usd,
                "executed":   executed,
                "dry_run":    self.dry_run,
            })

        if results:
            logger.info("AaveAvax: %d oportunidades (%d checadas)", len(results), checked)
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
    print("AAVE V3 AVALANCHE LIQUIDATOR — Smoke Test")
    print("=" * 60)

    bot = AaveLiquidatorAvaxBot()

    connected = bot._connected()
    print(f"\n[1] RPC Avalanche conectado: {connected}")
    if not connected:
        print("    AVISO: sem conectividade — verificar ALCHEMY_AVAX_URL no .env")
        sys.exit(0)

    try:
        bloco = bot.w3.eth.block_number
        print(f"[2] Bloco actual Avalanche: {bloco:,}")
    except Exception as exc:
        print(f"[2] Bloco: ERRO — {exc}")
        sys.exit(1)

    reserves = bot._reserves_list()
    print(f"[3] Reserves Aave V3 Avalanche: {len(reserves)} activos")
    for r in reserves[:5]:
        print(f"    {r}")
    if len(reserves) > 5:
        print(f"    … (+{len(reserves) - 5} mais)")

    avax_px = bot._avax_price()
    print(f"[4] AVAX price (oracle Aave): ${avax_px:,.4f}")

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
