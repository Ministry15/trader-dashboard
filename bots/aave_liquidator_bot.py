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

from web3 import Web3
from web3.exceptions import ContractLogicError

from utils.config import get_env, get_settings
from utils.database import init_db, record_liquidation_opportunity
from utils.logger import get_logger
from utils.notifier import TelegramNotifier

logger = get_logger(__name__)

# ── Aave V3 Base — endereços ────────────────────────────────────────────────

POOL_ADDRESS       = Web3.to_checksum_address("0xA238Dd80C259a72e81d7e4664a9801593F98d1c5")
ORACLE_ADDRESS     = Web3.to_checksum_address("0x2Cc0Fc26eD4563A5ce5e8bdcfe1a2878676Ae156")
FLASH_LIQ_ADDRESS  = Web3.to_checksum_address("0x9531F6F28202B7E83717b31992035F769046135B")
BASE_CHAIN_ID      = 8453
WETH_BASE          = "0x4200000000000000000000000000000000000006"

# Bonus de liquidação confirmados (Aave V3 Base governance proposal)
_BONUS: dict[str, float] = {
    "0x4200000000000000000000000000000000000006": 0.050,  # WETH  → 5%
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": 0.050,  # USDC  → 5%
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca": 0.050,  # USDbC → 5%
    "0x2ae3f1ec7f1f5012cfeab0185bfc7aa3cf0dec22": 0.075,  # cbETH → 7.5%
    "0xc1cba3fcea344f92d9239c08c0568f6f2f0ee452": 0.070,  # wstETH→ 7%
}
_DEFAULT_BONUS     = 0.050  # 5% conservador

_GAS_UNITS         = 500_000   # estimativa para flash loan + liquidação
_ORACLE_DECIMALS   = 8         # Aave oracle: USD com 8 decimais
_HF_DECIMALS       = 18        # healthFactor em ray (1e18)
_ACCOUNT_DECIMALS  = 8         # totalCollateralBase/totalDebtBase em USD×1e8

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

_FLASH_LIQ_ABI = [
    {
        "inputs": [
            {"name": "debtAsset",      "type": "address"},
            {"name": "collateralAsset","type": "address"},
            {"name": "borrower",       "type": "address"},
            {"name": "debtAmount",     "type": "uint256"},
        ],
        "name": "executeFlashLiquidation",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
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

        rpc_url = get_env("ALCHEMY_BASE_URL") or "https://mainnet.base.org"
        self.dry_run     : bool  = str(get_env("DRY_RUN", "true")).lower() not in ("false", "0", "no")
        self.hf_threshold: float = float(self.cfg.get("health_factor_threshold", 1.05))
        self.min_profit  : float = float(self.cfg.get("min_profit_usd", 5.0))
        self.scan_blocks : int   = int(self.cfg.get("borrower_scan_blocks", 50_000))
        self.max_per_tick: int   = int(self.cfg.get("max_positions_per_tick", 50))

        self.w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))

        self.pool    = self.w3.eth.contract(address=POOL_ADDRESS,      abi=_POOL_ABI)
        self.oracle  = self.w3.eth.contract(address=ORACLE_ADDRESS,    abi=_ORACLE_ABI)
        self.flash   = self.w3.eth.contract(address=FLASH_LIQ_ADDRESS, abi=_FLASH_LIQ_ABI)

        self._borrowers: set[str] = set()
        self._scan_from: int = 0
        self._reserves : list[str] = []
        self._eth_price_cache: float = 2000.0
        self._eth_price_ts   : float = 0.0

        self.notifier = TelegramNotifier(self.settings)
        init_db()

        logger.info(
            "AaveLiquidatorBot: rpc=%s dry_run=%s hf<%.2f min_profit=$%.2f",
            rpc_url.split("//")[-1].split("/")[0], self.dry_run,
            self.hf_threshold, self.min_profit,
        )

    # ── helpers ──────────────────────────────────────────────────────────────

    def _connected(self) -> bool:
        try:
            return self.w3.is_connected()
        except Exception:
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
                  col_usd: float, debt_usd: float) -> LiqOpportunity:
        reserves = self._reserves_list()
        # Usar primeiro par disponível como proxy (DRY_RUN)
        debt_asset      = reserves[0] if reserves else WETH_BASE
        collateral_asset = reserves[1] if len(reserves) > 1 else reserves[0] if reserves else WETH_BASE

        bonus            = _BONUS.get(debt_asset.lower(), _DEFAULT_BONUS)
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
        record_liquidation_opportunity(
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

    def _execute_live(self, opp: LiqOpportunity) -> str | None:
        """Executa flash loan liquidation via contrato deployado."""
        try:
            pk   = get_env("BSC_PRIVATE_KEY") or ""
            acct = self.w3.eth.account.from_key(pk)
            # Converter USD → unidades de token (aproximação via oracle)
            debt_oracle  = self.oracle.functions.getAssetPrice(
                Web3.to_checksum_address(opp.debt_asset)).call()
            debt_units   = int(opp.debt_to_cover_usd * 10 ** _ORACLE_DECIMALS
                               / (debt_oracle / 10 ** _ORACLE_DECIMALS))

            tx = self.flash.functions.executeFlashLiquidation(
                Web3.to_checksum_address(opp.debt_asset),
                Web3.to_checksum_address(opp.collateral_asset),
                Web3.to_checksum_address(opp.borrower),
                debt_units,
            ).build_transaction({
                "from":     acct.address,
                "chainId":  BASE_CHAIN_ID,
                "gas":      _GAS_UNITS,
                "gasPrice": self.w3.eth.gas_price,
                "nonce":    self.w3.eth.get_transaction_count(acct.address),
            })
            signed   = acct.sign_transaction(tx)
            tx_hash  = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            logger.info("LIQUIDAÇÃO TX: %s", tx_hash.hex())
            return tx_hash.hex()
        except Exception as exc:
            logger.error("Falha ao executar liquidação: %s", exc)
            return None

    # ── tick ─────────────────────────────────────────────────────────────────

    def tick(self) -> list[dict]:
        if not self._connected():
            logger.warning("AaveLiquidator: sem ligação ao RPC Base — tick saltado")
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
                "LIQUIDAÇÃO %s HF=%.4f dívida=$%.2f colateral=$%.2f lucro≈$%.4f dry=%s",
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
            logger.info("AaveLiquidator: %d oportunidades (%d checadas)", len(results), checked)
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
