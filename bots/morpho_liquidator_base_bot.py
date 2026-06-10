"""Bot de liquidações Morpho Blue na chain Base.

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

Contrato Morpho Blue Base (chain 8453):
  0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import requests
from web3 import Web3
from web3.exceptions import ContractLogicError

from utils.config import get_env, get_settings
from utils.database import init_db, upsert_liquidation_opportunity
from utils.logger import get_logger
from utils.notifier import TelegramNotifier

logger = get_logger(__name__)

# ── Morpho Blue Base ───────────────────────────────────────────────────────────

MORPHO_ADDRESS    = Web3.to_checksum_address("0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb")
BASE_CHAIN_ID     = 8453
_ORACLE_SCALE     = 10 ** 36   # Morpho oracles: price × 1e36 (já inclui decimal scaling)
_LLTV_SCALE       = 10 ** 18   # LLTV em WAD
_GAS_UNITS        = 350_000    # liquidate() na Base
_FALLBACK_RPC_1   = "https://base.drpc.org"
_FALLBACK_RPC_2   = "https://base.publicnode.com"

_HF_LIQUIDATABLE = 1.0      # Morpho: só liquidável quando HF < 1.0
_DEBT_MIN_USD    = 500.0
_DEBT_MAX_USD    = 50_000.0
_BLACKLIST_FAILS = 3

# Stablecoins Base (minúsculas): (symbol, decimals, usd_price)
_STABLE_TOKENS: dict[str, tuple[str, int, float]] = {
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": ("USDC",   6, 1.0),
    "0xfde4c96c8593536e31f229ea8f37b2ada2699bb2": ("USDT",   6, 1.0),
    "0x50c5725949a6f0c72e6c4a641f24049a917db0cb": ("DAI",   18, 1.0),
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca": ("USDbC",  6, 1.0),
}
_WETH_ADDRESS = "0x4200000000000000000000000000000000000006"

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
        # Borrow(bytes32 indexed id, address caller, address indexed onBehalf,
        #        address indexed receiver, uint256 assets, uint256 shares)
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
        # Liquidate(bytes32 indexed id, address indexed caller,
        #           address indexed borrower, uint256 repaidAssets, uint256 repaidShares,
        #           uint256 seizedAssets, uint256 badDebtAssets, uint256 badDebtShares)
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
class LiqOpportunityMorpho:
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

class MorphoLiquidatorBaseBot:
    """Monitoriza e (em modo live) executa liquidações Morpho Blue na Base."""

    def __init__(self, settings: dict | None = None):
        self.settings = settings or get_settings()
        self.cfg = self.settings.get("bots", {}).get("morpho_liquidator_base", {})

        primary_rpc = get_env("ALCHEMY_BASE_URL") or _FALLBACK_RPC_1
        self._rpc_urls: list[str] = [primary_rpc, _FALLBACK_RPC_1, _FALLBACK_RPC_2]
        self._active_rpc: str = primary_rpc

        self.dry_run      : bool  = False
        self.hf_threshold : float = float(self.cfg.get("health_factor_threshold", 1.2))
        self.min_profit   : float = float(self.cfg.get("min_profit_usd", 8.0))
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
        self._token_cache : dict[str, tuple[str, int, float]] = {}
        self._oracle_cache: dict[str, tuple[int, float]] = {}  # addr → (price_raw, ts)
        self._eth_price_cache: float = 2500.0
        self._eth_price_ts   : float = 0.0

        self._cooldown   : dict[str, float] = {}
        self._fail_counts: dict[str, int]   = {}
        self._blacklist  : dict[str, float] = {}

        self.notifier = TelegramNotifier(self.settings)
        init_db()

        logger.info(
            "MorphoBase: rpc=%s dry_run=%s hf<%.2f min_profit=$%.2f",
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
                # Limpar caches de oracle (ligados ao w3)
                self._oracle_cache.clear()
                logger.warning("MorphoBase: RPC → %s", url.split("//")[-1].split("/")[0])
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
                    logger.warning("MorphoBase: rate-limit (429) — fallback RPC")
                    if self._switch_rpc(self._active_rpc):
                        continue
                time.sleep(2 ** attempt)
            except Exception as exc:
                logger.debug("MorphoBase: erro RPC: %s", exc)
                if attempt == 0 and self._active_rpc != _FALLBACK_RPC_1:
                    if self._switch_rpc(self._active_rpc):
                        continue
                return False
        return False

    def _gas_price_gwei(self) -> float:
        try:
            return self.w3.eth.gas_price / 1e9
        except Exception:
            return 0.01

    def _eth_price(self) -> float:
        now = time.time()
        if now - self._eth_price_ts < 300:
            return self._eth_price_cache
        self._eth_price_ts = now
        return self._eth_price_cache

    def _token_info(self, address: str) -> tuple[str, int, float]:
        """(symbol, decimals, usd_price) — stablecoins e WETH são conhecidos."""
        addr = address.lower()
        if addr in self._token_cache:
            return self._token_cache[addr]
        if addr in _STABLE_TOKENS:
            info = _STABLE_TOKENS[addr]
            self._token_cache[addr] = info
            return info
        if addr == _WETH_ADDRESS:
            info = ("WETH", 18, self._eth_price())
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
            logger.debug("MorphoBase: oracle %s…: %s", oracle_address[:10], exc)
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
            logger.warning("MorphoBase: bloco actual: %s", exc)
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
                    mid     = e.args["id"].hex()
                    borrow  = e.args["onBehalf"].lower()
                    key     = (mid, borrow)
                    if key not in self._positions:
                        self._positions[key] = True
                        added += 1
                        # Carregar params do mercado se novo
                        if mid not in self._markets:
                            self._market_params(mid)
                from_block = to_block + 1
            except Exception as exc:
                logger.debug("MorphoBase: Borrow [%d..%d]: %s", from_block, to_block, exc)
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
            "MorphoBase: scan %d..%d +%d posições (total=%d, %d mercados)",
            scan_start, latest, added, len(self._positions), len(self._markets),
        )

    # ── análise de posição ────────────────────────────────────────────────────

    def _check_position(self, market_id: str, borrower: str) -> LiqOpportunityMorpho | None:
        market = self._market_params(market_id)
        if not market:
            return None

        mid_bytes  = bytes.fromhex(market_id)
        cs_borrow  = Web3.to_checksum_address(borrower)

        try:
            # 1. Posição do borrower
            pos = self.morpho.functions.position(mid_bytes, cs_borrow).call()
            borrow_shares = pos[1]   # uint128
            collateral    = pos[2]   # uint128

            if borrow_shares == 0:
                self._positions.pop((market_id, borrower), None)
                return None

            # 2. Estado do mercado (shares → assets)
            mkt = self.morpho.functions.market(mid_bytes).call()
            total_borrow_assets = mkt[2]
            total_borrow_shares = mkt[3]

            if total_borrow_shares == 0:
                return None

            borrow_raw = int(borrow_shares) * int(total_borrow_assets) // int(total_borrow_shares)
            if borrow_raw == 0:
                self._positions.pop((market_id, borrower), None)
                return None

            # 3. Oracle price (já inclui scaling de decimais)
            oracle_raw = self._oracle_price_raw(market["oracle"])
            if not oracle_raw:
                return None

            # 4. Health factor: (col × price × lltv) / (borrow × 1e36 × 1e18)
            lltv = int(market["lltv"])
            col  = int(collateral)

            if col == 0:
                # Sem colateral com dívida → liquidável (bad debt)
                if not borrow_raw:
                    self._positions.pop((market_id, borrower), None)
                    return None
                hf = 0.0
            else:
                hf = float(col * oracle_raw * lltv) / float(borrow_raw * _ORACLE_SCALE * _LLTV_SCALE)

            # 5. Filtrar posições saudáveis
            if hf >= self.hf_threshold:
                return None

            # 6. Valor em USD via token info do loan token
            loan_sym,  loan_dec,  loan_price = self._token_info(market["loanToken"])
            col_sym,   col_dec,   _          = self._token_info(market["collateralToken"])

            borrow_usd = (borrow_raw / 10 ** loan_dec) * loan_price

            # collateral_usd: derivado do oracle para evitar preços separados
            #   collateral_value_in_loan_raw = col × oraclePrice / 1e36
            collateral_value_loan_raw = float(col) * float(oracle_raw) / float(_ORACLE_SCALE)
            collateral_usd = (collateral_value_loan_raw / 10 ** loan_dec) * loan_price

            # 7. LIF e lucro estimado
            lltv_ratio   = lltv / _LLTV_SCALE
            lif          = min(1.15, 1.0 / (0.3 * lltv_ratio + 0.7))
            lif_bonus    = lif - 1.0
            gas_usd      = _GAS_UNITS * self._gas_price_gwei() * 1e-9 * self._eth_price()

            if hf < 1.0:
                net_profit = max(borrow_usd * lif_bonus - gas_usd, 0.0)
            else:
                # Lucro hipotético: quanto ganharíamos se 50% da dívida fosse liquidável
                net_profit = max(borrow_usd * 0.5 * lif_bonus - gas_usd, 0.0)

            return LiqOpportunityMorpho(
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
            logger.debug("MorphoBase: %s:%s…: %s", market_id[:8], borrower[:10], exc)
            return None

    # ── registo na BD ─────────────────────────────────────────────────────────

    def _record(self, opp: LiqOpportunityMorpho) -> None:
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
            chain="morpho_base",
        )
        logger.debug(
            "MorphoBase: BD %s id=%d %s/%s HF=%.4f liq=%s profit=$%.2f",
            "INS" if inserted else "UPD", rec_id,
            opp.collateral_symbol, opp.loan_symbol,
            opp.health_factor, is_liq, opp.estimated_profit_usd,
        )

    # ── execução live ─────────────────────────────────────────────────────────

    def _execute_live(self, opp: LiqOpportunityMorpho, nonce: int) -> str | None:
        market = self._market_params(opp.market_id)
        if not market:
            return None
        try:
            pk   = get_env("BSC_PRIVATE_KEY") or ""
            acct = self.w3.eth.account.from_key(pk)
            mp_tuple = (
                Web3.to_checksum_address(market["loanToken"]),
                Web3.to_checksum_address(market["collateralToken"]),
                Web3.to_checksum_address(market["oracle"]),
                Web3.to_checksum_address(market["irm"]),
                market["lltv"],
            )
            # Simulação obrigatória: eth_call antes de enviar TX (zero gas se falhar)
            try:
                self.morpho.functions.liquidate(
                    mp_tuple,
                    Web3.to_checksum_address(opp.borrower),
                    0, 0, b"",
                ).call({"from": acct.address})
            except Exception as sim_exc:
                self._cooldown[opp.position_key] = time.time() + 120
                logger.warning(
                    "MorphoBase: simulação falhou %s — cooldown 2min: %s",
                    opp.position_key, sim_exc,
                )
                return None

            tx = self.morpho.functions.liquidate(
                mp_tuple,
                Web3.to_checksum_address(opp.borrower),
                0,     # seizedAssets=0 → usar repaidShares
                0,     # repaidShares=0 → liquidar tudo via seizedAssets
                b"",
            ).build_transaction({
                "from":     acct.address,
                "chainId":  BASE_CHAIN_ID,
                "gas":      _GAS_UNITS,
                "gasPrice": self.w3.eth.gas_price,
                "nonce":    nonce,
            })
            signed  = acct.sign_transaction(tx)
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            logger.info("MorphoBase: LIQUIDATE TX: %s", tx_hash.hex())
            return tx_hash.hex()
        except Exception as exc:
            if "HEALTH_FACTOR_NOT_BELOW_THRESHOLD" in str(exc):
                self._cooldown[opp.position_key] = time.time() + 300
                logger.warning("MorphoBase: %s HF acima do threshold — cooldown 5min",
                               opp.position_key)
            else:
                logger.error("MorphoBase: falha ao executar liquidate: %s", exc)
            return None

    # ── tick ──────────────────────────────────────────────────────────────────

    def tick(self) -> list[dict]:
        if not self._connected():
            logger.warning("MorphoBase: sem ligação ao RPC Base — tick saltado")
            return []

        self._scan_borrowers()

        # Phase 1: construir lista elegível
        eligible: list[LiqOpportunityMorpho] = []
        checked = 0
        for (market_id, borrower) in list(self._positions.keys()):
            if checked >= self.max_per_tick:
                break
            checked += 1
            opp = self._check_position(market_id, borrower)
            if opp is None:
                continue
            eligible.append(opp)

        # filtro de tamanho de dívida
        eligible = [opp for opp in eligible
                    if _DEBT_MIN_USD <= opp.borrow_usd <= _DEBT_MAX_USD]

        _nonce = 0
        if not self.dry_run:
            _acct = self.w3.eth.account.from_key(get_env("BSC_PRIVATE_KEY") or "")
            _bal_wei = self.w3.eth.get_balance(_acct.address)
            if _bal_wei < Web3.to_wei(0.005, 'ether'):
                logger.error(
                    "MorphoBase: saldo insuficiente (%.6f ETH < 0.005) — tick saltado",
                    _bal_wei / 1e18,
                )
                return []
            _nonce = self.w3.eth.get_transaction_count(_acct.address, "pending")

        _now_tick    = time.time()
        _n_liq       = sum(1 for opp in eligible if opp.health_factor < _HF_LIQUIDATABLE)
        _n_cooldown  = sum(1 for opp in eligible
                           if self._cooldown.get(opp.position_key, 0) > _now_tick)
        _n_blacklist = len(self._blacklist)
        logger.info(
            "MorphoBase Tick: %d elegíveis | %d liquidáveis (HF<1.0) | %d cooldown | %d blacklist",
            len(eligible), _n_liq, _n_cooldown, _n_blacklist,
        )

        results: list[dict] = []
        for opp in eligible:
            _pos_key = opp.position_key

            if _pos_key in self._blacklist:
                _bl_hf = self._blacklist[_pos_key]
                if abs(opp.health_factor - _bl_hf) / max(_bl_hf, 0.001) < 0.05:
                    logger.debug("MorphoBase: %s blacklisted (HF=%.4f) — saltado",
                                 _pos_key, opp.health_factor)
                    continue
                del self._blacklist[_pos_key]
                self._fail_counts.pop(_pos_key, None)
                logger.info("MorphoBase: %s saiu da blacklist (HF %.4f→%.4f)",
                            _pos_key, _bl_hf, opp.health_factor)

            is_liq = opp.health_factor < _HF_LIQUIDATABLE
            logger.info(
                "MorphoBase %s %s %s/%s HF=%.4f debt=$%.2f col=$%.2f lucro≈$%.4f dry=%s",
                "LIQUIDÁVEL" if is_liq else "vigiar",
                opp.borrower[:10] + "…", opp.collateral_symbol, opp.loan_symbol,
                opp.health_factor, opp.borrow_usd, opp.collateral_usd,
                opp.estimated_profit_usd, self.dry_run,
            )

            tx_hash, executed = None, False
            if not self.dry_run and is_liq and opp.estimated_profit_usd >= self.min_profit:
                _now   = time.time()
                _until = self._cooldown.get(_pos_key, 0)
                if _until > _now:
                    logger.debug("MorphoBase: %s em cooldown (%.0fs restantes) — saltado",
                                 _pos_key, _until - _now)
                else:
                    tx_hash = self._execute_live(opp, nonce=_nonce)
                    if tx_hash is None:
                        _cnt = self._fail_counts.get(_pos_key, 0) + 1
                        self._fail_counts[_pos_key] = _cnt
                        if _cnt >= _BLACKLIST_FAILS:
                            self._blacklist[_pos_key] = opp.health_factor
                            logger.warning(
                                "MorphoBase: %s adicionado à blacklist (%d falhas consecutivas)",
                                _pos_key, _cnt,
                            )
                    else:
                        self._fail_counts.pop(_pos_key, None)
                executed = tx_hash is not None
                if executed:
                    _nonce += 1
                    self.notifier.notify(
                        "trade_executed",
                        f"🔵 Morpho Base LIQUIDATE {opp.borrower[:10]}… "
                        f"{opp.collateral_symbol}/{opp.loan_symbol} "
                        f"lucro≈${opp.estimated_profit_usd:.2f} | tx={tx_hash[:20]}…",
                    )

            self._record(opp)
            results.append({
                "borrower":    opp.borrower,
                "market_id":   opp.market_id[:10],
                "loan":        opp.loan_symbol,
                "collateral":  opp.collateral_symbol,
                "hf":          opp.health_factor,
                "liquidatable": is_liq,
                "borrow_usd":  opp.borrow_usd,
                "profit_usd":  opp.estimated_profit_usd,
            })

        liquidable = sum(1 for r in results if r["liquidatable"])
        logger.info(
            "MorphoBase: tick — %d liquidáveis / %d em vigilância (%d checados, %d posições, %d mercados)",
            liquidable, len(results) - liquidable, checked,
            len(self._positions), len(self._markets),
        )
        return results
