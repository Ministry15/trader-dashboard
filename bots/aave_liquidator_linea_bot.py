"""Bot de liquidações Aave V3 na chain Linea.

Estratégia idêntica ao aave_liquidator_op_bot.py, mas independente:
  1. Mantém lista de mutuários via scanning de eventos Borrow
  2. Verifica health factor de cada posição a cada poll_seconds
  3. Posições com HF < health_factor_threshold (default: 1.2):
       – Calcula dívida a cobrir (50% do total, limite Aave)
       – Estima colateral a receber (dívida × (1 + bonus))
       – Estima custo de gas (Linea usa ETH)
       – Calcula lucro líquido em USD
  4. DRY_RUN=true: regista oportunidade na BD, NÃO executa
  5. DRY_RUN=false: chama contrato de flash loan para executar
       (requer flash_loan_contract em settings.yaml aave_liquidator_linea)

Contratos Aave V3 Linea (mainnet, chain 59144):
  Pool:        0x2f9bB73a8e98793e26Cb2F6C4ad037BDf1C6B269
  PriceOracle: 0x3f3f5dF88dC9F13eac63DF89EC16ef6e7E25DdE7

RPC: LINEA_RPC_URL do .env (fallback: https://linea.drpc.org)
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

# ── Aave V3 Linea — endereços ─────────────────────────────────────────────────

POOL_ADDRESS     = Web3.to_checksum_address("0x2f9bB73a8e98793e26Cb2F6C4ad037BDf1C6B269")
ORACLE_ADDRESS   = Web3.to_checksum_address("0x3f3f5dF88dC9F13eac63DF89EC16ef6e7E25DdE7")
LINEA_CHAIN_ID   = 59144
WETH_LINEA       = "0xe5D7C2a44FfDDf6b295A15c148167daaAf5Cf34"   # WETH nativo Linea
_LINEA_FALLBACK_RPC = "https://linea.drpc.org"

# Bonus de liquidação Aave V3 Linea (valores conservadores)
_BONUS: dict[str, float] = {
    "0xe5d7c2a44ffddf6b295a15c148167daaaf5cf34": 0.050,  # WETH    → 5%
    "0x176211869ca2b568f2a7d4ee941e073a821ee1ff": 0.050,  # USDC    → 5%
    "0xa219439258ca9da29e9cc4ce5596924745e12b93": 0.050,  # USDT    → 5%
    "0x3aab2285ddcddad8edf438c1bab47e1a9d05a9b4": 0.075,  # wBTC    → 7.5%
    "0xb5bedd42000b71fdde22d3ee8a79bd49a568fc8f": 0.075,  # wstETH  → 7.5%
}
_DEFAULT_BONUS    = 0.050  # 5% conservador

_HF_LIQUIDATABLE = 1.0
_DEBT_MIN_USD    = 500.0
_DEBT_MAX_USD    = 50_000.0
_BLACKLIST_FAILS = 3

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
class LiqOpportunityLinea:
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

class AaveLiquidatorLineaBot:
    """Monitoriza e (em modo live) executa liquidações Aave V3 na Linea."""

    def __init__(self, settings: dict | None = None):
        self.settings = settings or get_settings()
        self.cfg = self.settings.get("bots", {}).get("aave_liquidator_linea", {})

        primary_rpc = get_env("LINEA_RPC_URL") or _LINEA_FALLBACK_RPC
        self._rpc_urls: list[str] = [primary_rpc, _LINEA_FALLBACK_RPC]
        self._active_rpc: str = primary_rpc

        self.dry_run      : bool  = False
        self.hf_threshold : float = float(self.cfg.get("health_factor_threshold", 1.2))
        self.min_profit   : float = float(self.cfg.get("min_profit_usd", 5.0))
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
        self._eth_price_cache: float = 2500.0
        self._eth_price_ts   : float = 0.0
        self._cooldown       : dict[str, float] = {}
        self._fail_counts    : dict[str, int]   = {}
        self._blacklist      : dict[str, float] = {}

        self.notifier = TelegramNotifier(self.settings)
        init_db()

        if not flash_addr:
            self.dry_run = True
            logger.warning(
                "AaveLinea: flash_loan_contract não configurado — modo DRY_RUN forçado para live"
            )

        logger.info(
            "AaveLinea: rpc=%s dry_run=%s hf<%.2f min_profit=$%.2f",
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
                self.w3     = test_w3
                self._active_rpc = url
                self.pool   = self.w3.eth.contract(address=POOL_ADDRESS,   abi=_POOL_ABI)
                self.oracle = self.w3.eth.contract(address=ORACLE_ADDRESS, abi=_ORACLE_ABI)
                if self.flash is not None:
                    self.flash = self.w3.eth.contract(
                        address=self.flash.address, abi=_FLASH_LIQ_ABI
                    )
                logger.warning("AaveLinea: RPC trocado para fallback: %s",
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
                            "AaveLinea: rate-limit (429) — a tentar fallback RPC (tentativa %d/3)",
                            attempt + 1,
                        )
                        if self._switch_rpc(self._active_rpc):
                            continue
                    wait = 2 ** attempt
                    logger.debug("AaveLinea: 429 rate-limit, aguardar %ds…", wait)
                    time.sleep(wait)
                else:
                    logger.debug("AaveLinea: HTTP %s ao verificar ligação: %s", status, exc)
                    return False
            except Exception as exc:
                logger.debug("AaveLinea: erro ao verificar ligação RPC: %s", exc)
                if attempt == 0 and self._active_rpc != _LINEA_FALLBACK_RPC:
                    if self._switch_rpc(self._active_rpc):
                        continue
                return False
        return False

    def _eth_price(self) -> float:
        now = time.time()
        if now - self._eth_price_ts < 300:
            return self._eth_price_cache
        try:
            raw = self.oracle.functions.getAssetPrice(
                Web3.to_checksum_address(WETH_LINEA)).call()
            self._eth_price_cache = raw / 10 ** _ORACLE_DECIMALS
            self._eth_price_ts    = now
        except Exception as exc:
            logger.debug("AaveLinea: oracle ETH price falhou: %s — cache $%.2f",
                         exc, self._eth_price_cache)
        return self._eth_price_cache

    def _gas_price_gwei(self) -> float:
        try:
            return self.w3.eth.gas_price / 1e9
        except Exception:
            return 0.1   # Linea: ~0.1 Gwei típico

    def _reserves_list(self) -> list[str]:
        if not self._reserves:
            try:
                self._reserves = [
                    Web3.to_checksum_address(r)
                    for r in self.pool.functions.getReservesList().call()
                ]
                logger.info("AaveLinea: Reserves Aave Linea: %d activos", len(self._reserves))
            except Exception as exc:
                logger.warning("AaveLinea: getReservesList falhou: %s", exc)
        return self._reserves

    # ── descoberta de mutuários ───────────────────────────────────────────────

    def _scan_borrowers(self) -> None:
        if not self._connected():
            return
        try:
            latest = self.w3.eth.block_number
        except Exception as exc:
            logger.warning("AaveLinea: não consegui ler bloco actual: %s", exc)
            return

        from_block = self._scan_from or max(0, latest - self.scan_blocks)
        scan_start = from_block
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
                logger.debug("AaveLinea: Borrow events [%d..%d]: %s", from_block, to_block, exc)
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

        logger.info("AaveLinea: scan blocos %d..%d +%d novos mutuários (total=%d)",
                    scan_start, latest, added, len(self._borrowers))

    # ── análise de posições ───────────────────────────────────────────────────

    def _check_health(self, address: str) -> tuple[float, float, float] | None:
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
            logger.debug("AaveLinea: getUserAccountData(%s…): %s", address[:10], exc)
            return None

    def _estimate(self, borrower: str, hf: float,
                  col_usd: float, debt_usd: float) -> LiqOpportunityLinea:
        reserves = self._reserves_list()
        debt_asset       = reserves[0] if reserves else WETH_LINEA
        collateral_asset = reserves[1] if len(reserves) > 1 else reserves[0] if reserves else WETH_LINEA

        bonus          = _BONUS.get(debt_asset.lower(), _DEFAULT_BONUS)
        debt_to_cover  = debt_usd * 0.50
        col_to_receive = debt_to_cover * (1.0 + bonus)
        gas_usd        = _GAS_UNITS * self._gas_price_gwei() * 1e-9 * self._eth_price()
        net_profit     = col_to_receive - debt_to_cover - gas_usd

        return LiqOpportunityLinea(
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

    def _record(self, opp: LiqOpportunityLinea, executed: bool,
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
            chain="linea",
        )
        action = "INSERT" if inserted else "UPDATE"
        logger.debug("AaveLinea: BD %s id=%d %s HF=%.4f",
                     action, rec_id, opp.borrower[:10], opp.health_factor)

    def _execute_live(self, opp: LiqOpportunityLinea, nonce: int) -> str | None:
        if self.flash is None:
            logger.error(
                "AaveLinea: flash_loan_contract não configurado — não é possível executar live"
            )
            return None
        try:
            pk   = get_env("BSC_PRIVATE_KEY") or ""
            acct = self.w3.eth.account.from_key(pk)
            debt_oracle = self.oracle.functions.getAssetPrice(
                Web3.to_checksum_address(opp.debt_asset)).call()
            debt_units  = int(opp.debt_to_cover_usd * 10 ** _ORACLE_DECIMALS
                              / (debt_oracle / 10 ** _ORACLE_DECIMALS))

            # Simulação obrigatória: eth_call antes de enviar TX
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
                    "AaveLinea: simulação falhou %s — cooldown 2min: %s",
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
                "chainId":  LINEA_CHAIN_ID,
                "gas":      _GAS_UNITS,
                "gasPrice": int(self.w3.eth.gas_price * 1.15),
                "nonce":    nonce,
            })
            signed  = acct.sign_transaction(tx)
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            logger.info("AaveLinea: LIQUIDAÇÃO TX: %s", tx_hash.hex())
            return tx_hash.hex()
        except Exception as exc:
            if "HEALTH_FACTOR_NOT_BELOW_THRESHOLD" in str(exc):
                self._cooldown[opp.borrower.lower()] = time.time() + 300
                logger.warning(
                    "AaveLinea: %s HF acima do threshold — cooldown 5min",
                    opp.borrower[:10] + "…",
                )
            else:
                logger.error("AaveLinea: falha ao executar liquidação: %s", exc)
            return None

    # ── tick ─────────────────────────────────────────────────────────────────

    def tick(self) -> list[dict]:
        if not self._connected():
            logger.warning("AaveLinea: sem ligação ao RPC Linea — tick saltado")
            return []

        self._scan_borrowers()

        candidates = list(self._borrowers)[:self.max_per_tick]
        if not candidates:
            return []

        # Health factors sequencialmente + filtros
        eligible: list[tuple[str, float, float, float]] = []
        for b in candidates:
            data = self._check_health(b)
            if data is None:
                continue
            hf, col_usd, debt_usd = data
            if hf < self.hf_threshold and _DEBT_MIN_USD <= debt_usd <= _DEBT_MAX_USD:
                eligible.append((b, hf, col_usd, debt_usd))

        # Nonce sequencial: 1 fetch por tick, incrementa por TX aceite
        _pk    = get_env("BSC_PRIVATE_KEY") or ""
        _acct  = self.w3.eth.account.from_key(_pk)
        _nonce = self.w3.eth.get_transaction_count(_acct.address, 'pending')

        # Saldo mínimo: suspende execução se ETH insuficiente
        if not self.dry_run:
            _bal_wei = self.w3.eth.get_balance(_acct.address)
            if _bal_wei < Web3.to_wei(0.005, 'ether'):
                logger.error(
                    "AaveLinea: saldo insuficiente (%.6f ETH < 0.005) — execução suspensa",
                    float(Web3.from_wei(_bal_wei, 'ether')),
                )
                return []

        _now_tick    = time.time()
        _n_liq       = sum(1 for _, hf, _, _ in eligible if hf < _HF_LIQUIDATABLE)
        _n_cooldown  = sum(1 for b, *_ in eligible if self._cooldown.get(b.lower(), 0) > _now_tick)
        _n_blacklist = len(self._blacklist)
        logger.info(
            "AaveLinea Tick: %d elegíveis | %d liquidáveis (HF<1.0) | %d cooldown | %d blacklist",
            len(eligible), _n_liq, _n_cooldown, _n_blacklist,
        )

        results: list[dict] = []
        for borrower, hf, col_usd, debt_usd in eligible:
            _b_low = borrower.lower()

            # Blacklist: salta se HF não mudou >5%
            if _b_low in self._blacklist:
                _bl_hf = self._blacklist[_b_low]
                if abs(hf - _bl_hf) / max(_bl_hf, 0.001) < 0.05:
                    logger.debug("AaveLinea: %s blacklisted (HF=%.4f) — saltado",
                                 borrower[:10] + "…", hf)
                    continue
                del self._blacklist[_b_low]
                self._fail_counts.pop(_b_low, None)
                logger.info("AaveLinea: %s saiu da blacklist (HF %.4f→%.4f)",
                            borrower[:10] + "…", _bl_hf, hf)

            opp = self._estimate(borrower, hf, col_usd, debt_usd)

            logger.info(
                "AaveLinea: LIQUIDAÇÃO %s HF=%.4f dívida=$%.2f colateral=$%.2f lucro≈$%.4f dry=%s",
                borrower[:10] + "…", hf, debt_usd, col_usd,
                opp.net_profit_usd, self.dry_run,
            )

            tx_hash, executed = None, False
            if not self.dry_run and hf < _HF_LIQUIDATABLE and opp.net_profit_usd >= self.min_profit:
                _until = self._cooldown.get(_b_low, 0)
                if _until > _now_tick:
                    logger.debug("AaveLinea: %s em cooldown (%.0fs) — saltado",
                                 borrower[:10] + "…", _until - _now_tick)
                else:
                    tx_hash = self._execute_live(opp, nonce=_nonce)
                    if tx_hash is None:
                        _cnt = self._fail_counts.get(_b_low, 0) + 1
                        self._fail_counts[_b_low] = _cnt
                        if _cnt >= _BLACKLIST_FAILS:
                            self._blacklist[_b_low] = hf
                            logger.warning(
                                "AaveLinea: %s adicionado à blacklist (%d falhas consecutivas)",
                                borrower[:10] + "…", _cnt,
                            )
                    else:
                        self._fail_counts.pop(_b_low, None)
                    executed = tx_hash is not None
                    if executed:
                        _nonce += 1
                        self.notifier.notify(
                            "trade_executed",
                            f"🔷 LIQUIDAÇÃO Linea executada {borrower[:10]}… "
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
            logger.info("AaveLinea: %d oportunidades (%d candidatos)", len(results), len(candidates))
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
    print("AAVE V3 LINEA LIQUIDATOR — Smoke Test")
    print("=" * 60)

    bot = AaveLiquidatorLineaBot()

    connected = bot._connected()
    print(f"\n[1] RPC Linea conectado: {connected}")
    if not connected:
        print("    AVISO: sem conectividade — verificar LINEA_RPC_URL no .env")
        sys.exit(0)

    try:
        bloco = bot.w3.eth.block_number
        print(f"[2] Bloco actual Linea: {bloco:,}")
    except Exception as exc:
        print(f"[2] Bloco: ERRO — {exc}")
        sys.exit(1)

    reserves = bot._reserves_list()
    print(f"[3] Reserves Aave V3 Linea: {len(reserves)} activos")
    for r in reserves[:5]:
        print(f"    {r}")
    if len(reserves) > 5:
        print(f"    … (+{len(reserves) - 5} mais)")

    eth_px = bot._eth_price()
    print(f"[4] ETH price (oracle Aave): ${eth_px:,.2f}")

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
    print("\nSMOKE OK")
