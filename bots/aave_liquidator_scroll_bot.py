"""Bot de liquidações Aave V3 na chain Scroll.

Estratégia idêntica ao aave_liquidator_op_bot.py, mas independente:
  1. Mantém lista de mutuários via scanning de eventos Borrow
  2. Verifica health factor de cada posição a cada poll_seconds
  3. Posições com HF < health_factor_threshold (default: 1.2):
       – Calcula dívida a cobrir (50% do total, limite Aave)
       – Estima colateral a receber (dívida × (1 + bonus))
       – Estima custo de gas (Scroll usa ETH)
       – Calcula lucro líquido em USD
  4. DRY_RUN=true: regista oportunidade na BD, NÃO executa
  5. DRY_RUN=false: chama contrato de flash loan para executar
       (requer flash_loan_contract em settings.yaml aave_liquidator_scroll)

Contratos Aave V3 Scroll (mainnet, chain 534352):
  Pool:        0x11fCfe756c05AD438e312a7fd934381537D3cFfe
  PriceOracle: 0x04421D8C506E2fA2371a08EfAaBf791F624054F3

RPC: SCROLL_RPC_URL do .env (fallback: https://scroll-mainnet.public.blastapi.io)
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

# ── Aave V3 Scroll — endereços ────────────────────────────────────────────────

POOL_ADDRESS      = Web3.to_checksum_address("0x11fCfe756c05AD438e312a7fd934381537D3cFfe")
ORACLE_ADDRESS    = Web3.to_checksum_address("0x04421D8C506E2fA2371a08EfAaBf791F624054F3")
SCROLL_CHAIN_ID   = 534352
WETH_SCROLL       = "0x5300000000000000000000000000000000000004"  # WETH nativo Scroll
_SCROLL_FALLBACK_RPC = "https://scroll.drpc.org"

# Bonus de liquidação Aave V3 Scroll (valores conservadores)
_BONUS: dict[str, float] = {
    "0x5300000000000000000000000000000000000004": 0.050,  # WETH    → 5%
    "0x06efdbff2a14a7c8e15944d1f4a48f9f95f663a4": 0.050,  # USDC    → 5%
    "0xf55bec9cafdbe8730f096aa55dad6d22d44099df": 0.050,  # USDT    → 5%
    "0x3c1bca5a656e69edcd0d4e36bebb3fcdaca60cf1": 0.075,  # wstETH  → 7.5%
    "0xcf7e09cdfb5e7a3439c73ce8f66a6a1b0d51e1a5": 0.075,  # rETH    → 7.5%
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
class LiqOpportunityScroll:
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

class AaveLiquidatorScrollBot:
    """Monitoriza e (em modo live) executa liquidações Aave V3 na Scroll."""

    def __init__(self, settings: dict | None = None):
        self.settings = settings or get_settings()
        self.cfg = self.settings.get("bots", {}).get("aave_liquidator_scroll", {})

        primary_rpc = get_env("SCROLL_RPC_URL") or _SCROLL_FALLBACK_RPC
        self._rpc_urls: list[str] = [primary_rpc, _SCROLL_FALLBACK_RPC]
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
                "AaveScroll: flash_loan_contract não configurado — modo DRY_RUN forçado para live"
            )

        logger.info(
            "AaveScroll: rpc=%s dry_run=%s hf<%.2f min_profit=$%.2f",
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
                logger.warning("AaveScroll: RPC trocado para fallback: %s",
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
                            "AaveScroll: rate-limit (429) — a tentar fallback RPC (tentativa %d/3)",
                            attempt + 1,
                        )
                        if self._switch_rpc(self._active_rpc):
                            continue
                    wait = 2 ** attempt
                    logger.debug("AaveScroll: 429 rate-limit, aguardar %ds…", wait)
                    time.sleep(wait)
                else:
                    logger.debug("AaveScroll: HTTP %s ao verificar ligação: %s", status, exc)
                    return False
            except Exception as exc:
                logger.debug("AaveScroll: erro ao verificar ligação RPC: %s", exc)
                if attempt == 0 and self._active_rpc != _SCROLL_FALLBACK_RPC:
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
                Web3.to_checksum_address(WETH_SCROLL)).call()
            self._eth_price_cache = raw / 10 ** _ORACLE_DECIMALS
            self._eth_price_ts    = now
        except Exception as exc:
            logger.debug("AaveScroll: oracle ETH price falhou: %s — cache $%.2f",
                         exc, self._eth_price_cache)
        return self._eth_price_cache

    def _gas_price_gwei(self) -> float:
        try:
            return self.w3.eth.gas_price / 1e9
        except Exception:
            return 0.1   # Scroll: ~0.1 Gwei típico

    def _reserves_list(self) -> list[str]:
        if not self._reserves:
            try:
                self._reserves = [
                    Web3.to_checksum_address(r)
                    for r in self.pool.functions.getReservesList().call()
                ]
                logger.info("AaveScroll: Reserves Aave Scroll: %d activos", len(self._reserves))
            except Exception as exc:
                logger.warning("AaveScroll: getReservesList falhou: %s", exc)
        return self._reserves

    # ── descoberta de mutuários ───────────────────────────────────────────────

    def _scan_borrowers(self) -> None:
        if not self._connected():
            return
        try:
            latest = self.w3.eth.block_number
        except Exception as exc:
            logger.warning("AaveScroll: não consegui ler bloco actual: %s", exc)
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
                logger.debug("AaveScroll: Borrow events [%d..%d]: %s", from_block, to_block, exc)
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

        logger.info("AaveScroll: scan blocos %d..%d +%d novos mutuários (total=%d)",
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
            logger.debug("AaveScroll: getUserAccountData(%s…): %s", address[:10], exc)
            return None

    def _estimate(self, borrower: str, hf: float,
                  col_usd: float, debt_usd: float) -> LiqOpportunityScroll:
        reserves = self._reserves_list()
        debt_asset       = reserves[0] if reserves else WETH_SCROLL
        collateral_asset = reserves[1] if len(reserves) > 1 else reserves[0] if reserves else WETH_SCROLL

        bonus          = _BONUS.get(debt_asset.lower(), _DEFAULT_BONUS)
        debt_to_cover  = debt_usd * 0.50
        col_to_receive = debt_to_cover * (1.0 + bonus)
        gas_usd        = _GAS_UNITS * self._gas_price_gwei() * 1e-9 * self._eth_price()
        net_profit     = col_to_receive - debt_to_cover - gas_usd

        return LiqOpportunityScroll(
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

    def _record(self, opp: LiqOpportunityScroll, executed: bool,
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
            chain="scroll",
        )
        action = "INSERT" if inserted else "UPDATE"
        logger.debug("AaveScroll: BD %s id=%d %s HF=%.4f",
                     action, rec_id, opp.borrower[:10], opp.health_factor)

    def _execute_live(self, opp: LiqOpportunityScroll, nonce: int) -> str | None:
        if self.flash is None:
            logger.error(
                "AaveScroll: flash_loan_contract não configurado — não é possível executar live"
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
                    "AaveScroll: simulação falhou %s — cooldown 2min: %s",
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
                "chainId":  SCROLL_CHAIN_ID,
                "gas":      _GAS_UNITS,
                "gasPrice": int(self.w3.eth.gas_price * 1.15),
                "nonce":    nonce,
            })
            signed  = acct.sign_transaction(tx)
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            logger.info("AaveScroll: LIQUIDAÇÃO TX: %s", tx_hash.hex())
            return tx_hash.hex()
        except Exception as exc:
            if "HEALTH_FACTOR_NOT_BELOW_THRESHOLD" in str(exc):
                self._cooldown[opp.borrower.lower()] = time.time() + 300
                logger.warning(
                    "AaveScroll: %s HF acima do threshold — cooldown 5min",
                    opp.borrower[:10] + "…",
                )
            else:
                logger.error("AaveScroll: falha ao executar liquidação: %s", exc)
            return None

    # ── tick ─────────────────────────────────────────────────────────────────

    def tick(self) -> list[dict]:
        if not self._connected():
            logger.warning("AaveScroll: sem ligação ao RPC Scroll — tick saltado")
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
                    "AaveScroll: saldo insuficiente (%.6f ETH < 0.005) — execução suspensa",
                    float(Web3.from_wei(_bal_wei, 'ether')),
                )
                return []

        _now_tick    = time.time()
        _n_liq       = sum(1 for _, hf, _, _ in eligible if hf < _HF_LIQUIDATABLE)
        _n_cooldown  = sum(1 for b, *_ in eligible if self._cooldown.get(b.lower(), 0) > _now_tick)
        _n_blacklist = len(self._blacklist)
        logger.info(
            "AaveScroll Tick: %d elegíveis | %d liquidáveis (HF<1.0) | %d cooldown | %d blacklist",
            len(eligible), _n_liq, _n_cooldown, _n_blacklist,
        )

        results: list[dict] = []
        for borrower, hf, col_usd, debt_usd in eligible:
            _b_low = borrower.lower()

            # Blacklist: salta se HF não mudou >5%
            if _b_low in self._blacklist:
                _bl_hf = self._blacklist[_b_low]
                if abs(hf - _bl_hf) / max(_bl_hf, 0.001) < 0.05:
                    logger.debug("AaveScroll: %s blacklisted (HF=%.4f) — saltado",
                                 borrower[:10] + "…", hf)
                    continue
                del self._blacklist[_b_low]
                self._fail_counts.pop(_b_low, None)
                logger.info("AaveScroll: %s saiu da blacklist (HF %.4f→%.4f)",
                            borrower[:10] + "…", _bl_hf, hf)

            opp = self._estimate(borrower, hf, col_usd, debt_usd)

            logger.info(
                "AaveScroll: LIQUIDAÇÃO %s HF=%.4f dívida=$%.2f colateral=$%.2f lucro≈$%.4f dry=%s",
                borrower[:10] + "…", hf, debt_usd, col_usd,
                opp.net_profit_usd, self.dry_run,
            )

            tx_hash, executed = None, False
            if not self.dry_run and hf < _HF_LIQUIDATABLE and opp.net_profit_usd >= self.min_profit:
                _until = self._cooldown.get(_b_low, 0)
                if _until > _now_tick:
                    logger.debug("AaveScroll: %s em cooldown (%.0fs) — saltado",
                                 borrower[:10] + "…", _until - _now_tick)
                else:
                    tx_hash = self._execute_live(opp, nonce=_nonce)
                    if tx_hash is None:
                        _cnt = self._fail_counts.get(_b_low, 0) + 1
                        self._fail_counts[_b_low] = _cnt
                        if _cnt >= _BLACKLIST_FAILS:
                            self._blacklist[_b_low] = hf
                            logger.warning(
                                "AaveScroll: %s adicionado à blacklist (%d falhas consecutivas)",
                                borrower[:10] + "…", _cnt,
                            )
                    else:
                        self._fail_counts.pop(_b_low, None)
                    executed = tx_hash is not None
                    if executed:
                        _nonce += 1
                        self.notifier.notify(
                            "trade_executed",
                            f"📜 LIQUIDAÇÃO Scroll executada {borrower[:10]}… "
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
            logger.info("AaveScroll: %d oportunidades (%d candidatos)", len(results), len(candidates))
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
    print("AAVE V3 SCROLL LIQUIDATOR — Smoke Test")
    print("=" * 60)

    bot = AaveLiquidatorScrollBot()

    connected = bot._connected()
    print(f"\n[1] RPC Scroll conectado: {connected}")
    if not connected:
        print("    AVISO: sem conectividade — verificar SCROLL_RPC_URL no .env")
        sys.exit(0)

    try:
        bloco = bot.w3.eth.block_number
        print(f"[2] Bloco actual Scroll: {bloco:,}")
    except Exception as exc:
        print(f"[2] Bloco: ERRO — {exc}")
        sys.exit(1)

    reserves = bot._reserves_list()
    print(f"[3] Reserves Aave V3 Scroll: {len(reserves)} activos")
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
