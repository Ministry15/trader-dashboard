"""Bot de liquidações Aave V3 na chain Optimism.

Estratégia idêntica ao aave_liquidator_arb_bot.py, mas independente:
  1. Mantém lista de mutuários via scanning de eventos Borrow
  2. Verifica health factor de cada posição a cada poll_seconds
  3. Posições com HF < health_factor_threshold (default: 1.2):
       – Calcula dívida a cobrir (50% do total, limite Aave)
       – Estima colateral a receber (dívida × (1 + bonus))
       – Estima custo de gas (Optimism usa ETH)
       – Calcula lucro líquido em USD
  4. DRY_RUN=true: regista oportunidade na BD, NÃO executa
  5. DRY_RUN=false: chama contrato de flash loan para executar
       (requer flash_loan_contract em settings.yaml aave_liquidator_op)

Contratos Aave V3 Optimism (mainnet, chain 10):
  Pool:        0x794a61358D6845594F94dc1DB02A252b5b4814aD
  PriceOracle: 0xD81eb3728a631871a7eBBaD631b5f424909f0c77

RPC: OPTIMISM_RPC_URL do .env (fallback: https://optimism.llamarpc.com)
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

# ── Aave V3 Optimism — endereços ─────────────────────────────────────────────

POOL_ADDRESS   = Web3.to_checksum_address("0x794a61358D6845594F94dc1DB02A252b5b4814aD")
ORACLE_ADDRESS = Web3.to_checksum_address("0xD81eb3728a631871a7eBBaD631b5f424909f0c77")
OP_CHAIN_ID    = 10
WETH_OP        = "0x4200000000000000000000000000000000000006"  # gas token para custo USD
_OP_FALLBACK_RPC = "https://optimism.drpc.org"

# Bonus de liquidação Aave V3 Optimism (valores conservadores)
_BONUS: dict[str, float] = {
    "0x4200000000000000000000000000000000000006": 0.050,  # WETH    → 5%
    "0x68f180fcce6836688e9084f035309e29bf0a2095": 0.075,  # WBTC    → 7.5%
    "0x0b2c639c533813f4aa9d7837caf62653d097ff85": 0.050,  # USDC    → 5%
    "0x7f5c764cbc14f9669b88837ca1490cca17c31607": 0.050,  # USDC.e  → 5%
    "0x94b008aa00579c1307b0ef2c499ad98a8ce58e58": 0.050,  # USDT    → 5%
    "0xda10009cbd5d07dd0cecc66161fc93d7c9000da1": 0.050,  # DAI     → 5%
    "0x1f32b1c2345538c0c6f582fcb022739c4a194ebb": 0.075,  # wstETH  → 7.5%
    "0x4200000000000000000000000000000000000042": 0.050,  # OP      → 5%
    "0x9bcef72be871e61ed4fbbc7630889bee758eb81d": 0.075,  # rETH    → 7.5%
}
_DEFAULT_BONUS    = 0.050  # 5% conservador

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
class LiqOpportunityOp:
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

class AaveLiquidatorOpBot:
    """Monitoriza e (em modo live) executa liquidações Aave V3 na Optimism."""

    def __init__(self, settings: dict | None = None):
        self.settings = settings or get_settings()
        self.cfg = self.settings.get("bots", {}).get("aave_liquidator_op", {})

        primary_rpc = get_env("OPTIMISM_RPC_URL") or _OP_FALLBACK_RPC
        self._rpc_urls: list[str] = [primary_rpc, _OP_FALLBACK_RPC]
        self._active_rpc: str = primary_rpc

        self.dry_run      : bool  = str(get_env("DRY_RUN", "true")).lower() not in ("false", "0", "no")
        self.hf_threshold : float = float(self.cfg.get("health_factor_threshold", 1.2))
        self.min_profit   : float = float(self.cfg.get("min_profit_usd", 8.0))
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

        self.notifier = TelegramNotifier(self.settings)
        init_db()

        if not flash_addr:
            logger.warning(
                "AaveOp: flash_loan_contract não configurado — modo DRY_RUN forçado para live"
            )

        logger.info(
            "AaveOp: rpc=%s dry_run=%s hf<%.2f min_profit=$%.2f",
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
                logger.warning("AaveOp: RPC trocado para fallback: %s",
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
                            "AaveOp: rate-limit (429) — a tentar fallback RPC (tentativa %d/3)",
                            attempt + 1,
                        )
                        if self._switch_rpc(self._active_rpc):
                            continue
                    wait = 2 ** attempt
                    logger.debug("AaveOp: 429 rate-limit, aguardar %ds…", wait)
                    time.sleep(wait)
                else:
                    logger.debug("AaveOp: HTTP %s ao verificar ligação: %s", status, exc)
                    return False
            except Exception as exc:
                logger.debug("AaveOp: erro ao verificar ligação RPC: %s", exc)
                if attempt == 0 and self._active_rpc != _OP_FALLBACK_RPC:
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
                Web3.to_checksum_address(WETH_OP)).call()
            self._eth_price_cache = raw / 10 ** _ORACLE_DECIMALS
            self._eth_price_ts    = now
        except Exception as exc:
            logger.debug("AaveOp: oracle ETH price falhou: %s — cache $%.2f",
                         exc, self._eth_price_cache)
        return self._eth_price_cache

    def _gas_price_gwei(self) -> float:
        try:
            return self.w3.eth.gas_price / 1e9
        except Exception:
            return 0.1   # Optimism: ~0.1 Gwei típico

    def _reserves_list(self) -> list[str]:
        if not self._reserves:
            try:
                self._reserves = [
                    Web3.to_checksum_address(r)
                    for r in self.pool.functions.getReservesList().call()
                ]
                logger.info("AaveOp: Reserves Aave Optimism: %d activos", len(self._reserves))
            except Exception as exc:
                logger.warning("AaveOp: getReservesList falhou: %s", exc)
        return self._reserves

    # ── descoberta de mutuários ───────────────────────────────────────────────

    def _scan_borrowers(self) -> None:
        """Varre eventos Borrow para encontrar mutuários activos."""
        if not self._connected():
            return
        try:
            latest = self.w3.eth.block_number
        except Exception as exc:
            logger.warning("AaveOp: não consegui ler bloco actual: %s", exc)
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
                logger.debug("AaveOp: Borrow events [%d..%d]: %s", from_block, to_block, exc)
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
            logger.info("AaveOp: Scan Borrow: +%d mutuários (total=%d, bloco=%d)",
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
            logger.debug("AaveOp: getUserAccountData(%s…): %s", address[:10], exc)
            return None

    def _estimate(self, borrower: str, hf: float,
                  col_usd: float, debt_usd: float) -> LiqOpportunityOp:
        reserves = self._reserves_list()
        debt_asset       = reserves[0] if reserves else WETH_OP
        collateral_asset = reserves[1] if len(reserves) > 1 else reserves[0] if reserves else WETH_OP

        bonus          = _BONUS.get(debt_asset.lower(), _DEFAULT_BONUS)
        debt_to_cover  = debt_usd * 0.50
        col_to_receive = debt_to_cover * (1.0 + bonus)
        gas_usd        = _GAS_UNITS * self._gas_price_gwei() * 1e-9 * self._eth_price()
        net_profit     = col_to_receive - debt_to_cover - gas_usd

        return LiqOpportunityOp(
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

    def _record(self, opp: LiqOpportunityOp, executed: bool,
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
            chain="op",
        )
        action = "INSERT" if inserted else "UPDATE"
        logger.debug("AaveOp: BD %s id=%d %s HF=%.4f",
                     action, rec_id, opp.borrower[:10], opp.health_factor)

    def _execute_live(self, opp: LiqOpportunityOp) -> str | None:
        """Executa flash loan liquidation via contrato deployado."""
        if self.flash is None:
            logger.error(
                "AaveOp: flash_loan_contract não configurado — não é possível executar live"
            )
            return None
        try:
            pk   = get_env("BSC_PRIVATE_KEY") or ""
            acct = self.w3.eth.account.from_key(pk)
            debt_oracle = self.oracle.functions.getAssetPrice(
                Web3.to_checksum_address(opp.debt_asset)).call()
            debt_units  = int(opp.debt_to_cover_usd * 10 ** _ORACLE_DECIMALS
                              / (debt_oracle / 10 ** _ORACLE_DECIMALS))

            tx = self.flash.functions.executeFlashLiquidation(
                Web3.to_checksum_address(opp.debt_asset),
                Web3.to_checksum_address(opp.collateral_asset),
                Web3.to_checksum_address(opp.borrower),
                debt_units,
            ).build_transaction({
                "from":     acct.address,
                "chainId":  OP_CHAIN_ID,
                "gas":      _GAS_UNITS,
                "gasPrice": self.w3.eth.gas_price,
                "nonce":    self.w3.eth.get_transaction_count(acct.address),
            })
            signed  = acct.sign_transaction(tx)
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            logger.info("AaveOp: LIQUIDAÇÃO TX: %s", tx_hash.hex())
            return tx_hash.hex()
        except Exception as exc:
            logger.error("AaveOp: falha ao executar liquidação: %s", exc)
            return None

    # ── tick ─────────────────────────────────────────────────────────────────

    def tick(self) -> list[dict]:
        if not self._connected():
            logger.warning("AaveOp: sem ligação ao RPC Optimism — tick saltado")
            return []

        self._scan_borrowers()

        results: list[dict] = []
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

            opp = self._estimate(borrower, hf, col_usd, debt_usd)

            logger.info(
                "AaveOp: LIQUIDAÇÃO %s HF=%.4f dívida=$%.2f colateral=$%.2f lucro≈$%.4f dry=%s",
                borrower[:10] + "…", hf, debt_usd, col_usd,
                opp.net_profit_usd, self.dry_run,
            )

            tx_hash, executed = None, False
            if not self.dry_run and opp.net_profit_usd >= self.min_profit:
                tx_hash  = self._execute_live(opp)
                executed = tx_hash is not None
                if executed:
                    self.notifier.notify(
                        "trade_executed",
                        f"🔴 LIQUIDAÇÃO Optimism executada {borrower[:10]}… "
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
            logger.info("AaveOp: %d oportunidades (%d checadas)", len(results), checked)
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
    print("AAVE V3 OPTIMISM LIQUIDATOR — Smoke Test")
    print("=" * 60)

    bot = AaveLiquidatorOpBot()

    connected = bot._connected()
    print(f"\n[1] RPC Optimism conectado: {connected}")
    if not connected:
        print("    AVISO: sem conectividade — verificar OPTIMISM_RPC_URL no .env")
        sys.exit(0)

    try:
        bloco = bot.w3.eth.block_number
        print(f"[2] Bloco actual Optimism: {bloco:,}")
    except Exception as exc:
        print(f"[2] Bloco: ERRO — {exc}")
        sys.exit(1)

    reserves = bot._reserves_list()
    print(f"[3] Reserves Aave V3 Optimism: {len(reserves)} activos")
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

    from utils.database import get_session
    from sqlalchemy import text
    with get_session() as s:
        n = s.execute(text("SELECT COUNT(*) FROM liquidation_opportunities")).scalar()
    print(f"\n[7] BD liquidation_opportunities: {n} registos")

    print("\nSMOKE OK")
