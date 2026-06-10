"""
moonwell_liquidator_base_bot.py — Liquidações Moonwell (Compound V2) em Base.

Fluxo:
  1. Scan eventos Borrow em cada mercado → set de mutuários conhecidos
  2. Por tick: getAccountLiquidity(borrower) → shortfall > 0 = liquidável
  3. Para cada posição liquidável: encontra o par dívida/colateral mais lucrativo
  4. Executa via MoonwellLiquidatorBase.sol (Aave flash loan → liquidateBorrow → redeem → swap)

Diferenças face ao aave_liquidator_bot.py:
  - Oracle Compound V2: getUnderlyingPrice(cToken) → USD × 10^(36−underlyingDecimals)
  - Bonus: 10% (vs 4.7–8% no Aave)
  - Close factor: 50% (max dívida a repagar por liquidação)
  - Sem health factor explícito — usa shortfall > 0 directamente
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import websockets

from web3 import Web3
from web3.exceptions import ContractLogicError

from utils.config import get_env
from utils.database import init_db, upsert_liquidation_opportunity
from utils.notifier import TelegramNotifier

logger = logging.getLogger(__name__)

# ── Log file ──────────────────────────────────────────────────────────────────
_LOG_DIR = "/opt/crypto_bsc/logs"
os.makedirs(_LOG_DIR, exist_ok=True)
_fh = logging.FileHandler(os.path.join(_LOG_DIR, "moonwell_base.log"))
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s",
                                   datefmt="%Y-%m-%d %H:%M:%S"))
logger.addHandler(_fh)

# ── RPC ───────────────────────────────────────────────────────────────────────
_BASE_RPC_PRIMARY  = "https://base.publicnode.com"
_BASE_RPC_FALLBACK = "https://mainnet.base.org"
_BASE_WSS_PRIMARY  = "wss://base.publicnode.com"
_BASE_WSS_FALLBACK = "wss://base.drpc.org"

# Scan incremental a cada N blocos (~2 min em Base com 2s por bloco)
_SCAN_INTERVAL_BLOCKS = 60

# ── Moonwell Base addresses ───────────────────────────────────────────────────
_COMPTROLLER = Web3.to_checksum_address("0xfBb21d0380beE3312B33c4353c8936a0F13EF26C")
_ORACLE      = Web3.to_checksum_address("0xEC942bE8A8114bFD0396A5052c36027f2cA6a9d0")

# Liquidation incentive = 10% (1.1e18), close factor = 50% (0.5e18)
_LIQ_INCENTIVE  = 1.10
_CLOSE_FACTOR   = 0.50

# Uniswap V3 fee tiers for collateral→debt swap (500 = 0.05%, 3000 = 0.3%)
# Pool fee by collateral underlying — covers common Moonwell pairs
_POOL_FEE: dict[str, int] = {
    # ETH-denominated collateral → any debt
    "0x4200000000000000000000000000000000000006": 500,   # WETH
    "0x2ae3f1ec7f1f5012cfeab0185bfc7aa3cf0dec22": 500,   # cbETH
    "0xc1cba3fcea344f92d9239c08c0568f6f2f0ee452": 500,   # wstETH
    "0xb6fe221fe9eeef5aba221c348ba20a1bf5e73624c": 500,  # rETH
    "0x04c0599ae5a44757c0af6f9ec3b93da8976c150a": 500,   # weETH
    "0xedfa23602d0ec14714057867a78d01e94176bea0": 500,   # wrsETH
    # BTC-denominated
    "0xcbb7c0000ab88b473b1f5afd9ec3b93da8976c5": 500,    # cbBTC (approx, 8 dec)
    # Stablecoin collateral
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": 100,   # USDC native
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca": 100,   # USDbC
    "0x50c5725949a6f0c72e6c4a641f24049a917db0cb": 100,   # DAI
}
_DEFAULT_POOL_FEE = 3000  # fallback for unlisted tokens

# Borrow event topic (no indexed params — borrower is in data)
_BORROW_TOPIC = "0x" + Web3.keccak(
    text="Borrow(address,uint256,uint256,uint256)"
).hex()

# ── Minimal ABIs ──────────────────────────────────────────────────────────────
_COMPTROLLER_ABI = [
    {"inputs": [], "name": "getAllMarkets",
     "outputs": [{"type": "address[]"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "address"}], "name": "getAccountLiquidity",
     "outputs": [{"type": "uint256"}, {"type": "uint256"}, {"type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "address"}], "name": "markets",
     "outputs": [{"type": "bool"}, {"type": "uint256"}, {"type": "bool"}],
     "stateMutability": "view", "type": "function"},
]

_CTOKEN_ABI = [
    {"inputs": [], "name": "symbol",
     "outputs": [{"type": "string"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "underlying",
     "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "decimals",
     "outputs": [{"type": "uint8"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "address"}], "name": "borrowBalanceStored",
     "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "address"}], "name": "balanceOf",
     "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "exchangeRateStored",
     "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "totalBorrows",
     "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
]

_ORACLE_ABI = [
    {"inputs": [{"type": "address"}], "name": "getUnderlyingPrice",
     "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
]

_ERC20_ABI = [
    {"inputs": [], "name": "symbol",
     "outputs": [{"type": "string"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "decimals",
     "outputs": [{"type": "uint8"}], "stateMutability": "view", "type": "function"},
]

_FLASH_ABI = [
    {
        "inputs": [
            {"name": "cDebt",       "type": "address"},
            {"name": "cCollateral", "type": "address"},
            {"name": "borrower",    "type": "address"},
            {"name": "repayAmount", "type": "uint256"},
            {"name": "poolFee",     "type": "uint24"},
        ],
        "name": "executeMoonwellLiquidation",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]


@dataclass
class MoonwellOpportunity:
    borrower:        str
    c_debt:          str
    c_collateral:    str
    debt_underlying: str
    col_underlying:  str
    debt_symbol:     str
    col_symbol:      str
    repay_amount:    int     # in underlying units
    repay_usd:       float
    col_seized_usd:  float
    net_profit_usd:  float
    pool_fee:        int


class MoonwellLiquidatorBaseBot:

    def __init__(self, settings: dict):
        self.settings = settings
        self.cfg = settings.get("bots", {}).get("moonwell_liquidator_base", {})

        primary_rpc = get_env("BASE_RPC_URL") or _BASE_RPC_PRIMARY
        self._rpc_urls  = [primary_rpc, _BASE_RPC_FALLBACK]
        self._active_rpc = primary_rpc

        self.hf_threshold = float(self.cfg.get("health_factor_threshold", 1.05))
        self.min_profit   = float(self.cfg.get("min_profit_usd", 10.0))
        self.scan_blocks  = int(self.cfg.get("borrower_scan_blocks", 500_000))
        self.max_per_tick = int(self.cfg.get("max_positions_per_tick", 100))

        flash_addr = self.cfg.get("flash_loan_contract", "")
        self.dry_run: bool = True  # safe default
        if flash_addr:
            self.dry_run = False
        else:
            logger.warning("MoonwellBase: flash_loan_contract não configurado — DRY_RUN forçado")

        self.w3          = Web3(Web3.HTTPProvider(primary_rpc, request_kwargs={"timeout": 30}))
        self.comptroller = self.w3.eth.contract(address=_COMPTROLLER, abi=_COMPTROLLER_ABI)
        self.oracle      = self.w3.eth.contract(address=_ORACLE, abi=_ORACLE_ABI)
        self.flash       = (
            self.w3.eth.contract(
                address=Web3.to_checksum_address(flash_addr), abi=_FLASH_ABI
            ) if flash_addr else None
        )

        # In-memory state
        self._markets:   list[str] = []
        self._borrowers: set[str]  = set()
        self._scan_from: int       = 0
        self._market_meta: dict[str, dict] = {}  # cToken → {underlying, decimals, symbol}

        self._cooldown:   dict[str, float] = {}
        self._blacklist:  dict[str, float] = {}
        self._fail_counts: dict[str, int]  = {}

        # WebSocket per-block
        self._block_queue:  queue.Queue = queue.Queue(maxsize=20)
        self._last_block:   int   = 0
        self._ws_last_seen: float = time.time()
        self._ws_stop       = threading.Event()
        self._ws_thread     = threading.Thread(
            target=self._ws_runner, daemon=True, name="moonwell-ws-listener")
        self._ws_thread.start()

        self.notifier = TelegramNotifier(settings)
        init_db()

        logger.info(
            "MoonwellBase: rpc=%s dry_run=%s min_profit=$%.2f scan_blocks=%d",
            primary_rpc.split("//")[-1].split("/")[0],
            self.dry_run, self.min_profit, self.scan_blocks,
        )

    # ── RPC failover ──────────────────────────────────────────────────────────
    def _switch_rpc(self, failed: str) -> bool:
        for url in self._rpc_urls:
            if url != failed:
                self.w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 30}))
                self.comptroller = self.w3.eth.contract(address=_COMPTROLLER, abi=_COMPTROLLER_ABI)
                self.oracle      = self.w3.eth.contract(address=_ORACLE, abi=_ORACLE_ABI)
                if self.flash:
                    self.flash = self.w3.eth.contract(
                        address=self.flash.address, abi=_FLASH_ABI)
                self._active_rpc = url
                logger.warning("MoonwellBase: RPC → %s", url.split("//")[-1].split("/")[0])
                return True
        return False

    # ── Market metadata (cached) ──────────────────────────────────────────────
    def _load_markets(self) -> None:
        if self._markets:
            return
        for attempt, rpc in enumerate(self._rpc_urls):
            try:
                w3tmp = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 20}))
                comp  = w3tmp.eth.contract(address=_COMPTROLLER, abi=_COMPTROLLER_ABI)
                self._markets = [Web3.to_checksum_address(m)
                                 for m in comp.functions.getAllMarkets().call()]
                if rpc != self._active_rpc:
                    self._switch_rpc(self._active_rpc)
                logger.info("MoonwellBase: %d mercados carregados via %s",
                            len(self._markets), rpc.split("//")[-1].split("/")[0])
                break
            except Exception as e:
                logger.warning("MoonwellBase: getAllMarkets erro (%s): %s",
                               rpc.split("//")[-1].split("/")[0], e)
        if not self._markets:
            return

        for mkt in self._markets:
            try:
                ct      = self.w3.eth.contract(address=mkt, abi=_CTOKEN_ABI)
                sym     = ct.functions.symbol().call()
                und_addr = Web3.to_checksum_address(ct.functions.underlying().call())
                und_ct   = self.w3.eth.contract(address=und_addr, abi=_ERC20_ABI)
                und_dec  = und_ct.functions.decimals().call()
                und_sym  = und_ct.functions.symbol().call()
                self._market_meta[mkt] = {
                    "symbol":     sym,
                    "underlying": und_addr,
                    "decimals":   und_dec,
                    "und_symbol": und_sym,
                }
            except Exception as e:
                logger.debug("MoonwellBase: meta erro %s: %s", mkt[:10], e)

    # ── Oracle price (USD, adjusted for decimals) ─────────────────────────────
    def _price_usd(self, ctoken: str, decimals: int) -> float:
        """Returns price of 1 underlying token in USD."""
        try:
            raw = self.oracle.functions.getUnderlyingPrice(
                Web3.to_checksum_address(ctoken)
            ).call()
            # Compound V2 oracle: price × 10^(36 − underlyingDecimals)
            return raw / (10 ** (36 - decimals))
        except Exception:
            return 0.0

    # ── Scan Borrow events ────────────────────────────────────────────────────
    def _scan_borrowers(self) -> None:
        """Scan incremental: max 20k blocos por tick para não bloquear."""
        self._load_markets()
        if not self._markets:
            return
        try:
            latest = self.w3.eth.block_number
        except Exception:
            return

        if self._scan_from == 0:
            self._scan_from = max(0, latest - self.scan_blocks)

        # Limita a 20k blocos por chamada → scan inicial completa em ~25 ticks (~12min)
        scan_end  = min(self._scan_from + 20_000 - 1, latest)
        new_count = 0

        for mkt in self._markets:
            cur = self._scan_from
            while cur <= scan_end:
                end = min(cur + 2000 - 1, scan_end)
                try:
                    logs = self.w3.eth.get_logs({
                        "fromBlock": cur,
                        "toBlock":   end,
                        "address":   mkt,
                        "topics":    [_BORROW_TOPIC],
                    })
                    for log in logs:
                        raw = bytes(log["data"])
                        if len(raw) >= 32:
                            borrower = Web3.to_checksum_address(
                                "0x" + raw[:32].hex()[-40:]
                            )
                            if borrower not in self._borrowers:
                                self._borrowers.add(borrower)
                                new_count += 1
                except Exception:
                    pass
                cur = end + 1

        progress_pct = min(100, int((scan_end - (latest - self.scan_blocks))
                                    / self.scan_blocks * 100))
        self._scan_from = scan_end + 1
        if new_count or progress_pct % 20 == 0:
            logger.info("MoonwellBase: scan %d%% completo | +%d mutuários (total=%d)",
                        progress_pct, new_count, len(self._borrowers))

    # ── Check single borrower ─────────────────────────────────────────────────
    def _check_borrower(self, borrower: str) -> Optional[MoonwellOpportunity]:
        """Returns best opportunity for this borrower, or None."""
        try:
            err, liquidity, shortfall = self.comptroller.functions.getAccountLiquidity(
                borrower
            ).call()
            if err != 0 or shortfall == 0:
                return None
        except Exception:
            return None

        # Find all debt and collateral positions
        best: Optional[MoonwellOpportunity] = None
        for c_debt in self._markets:
            meta_debt = self._market_meta.get(c_debt)
            if not meta_debt:
                continue
            try:
                ct_debt    = self.w3.eth.contract(address=c_debt, abi=_CTOKEN_ABI)
                borrow_raw = ct_debt.functions.borrowBalanceStored(borrower).call()
                if borrow_raw == 0:
                    continue
            except Exception:
                continue

            debt_dec   = meta_debt["decimals"]
            debt_price = self._price_usd(c_debt, debt_dec)
            if debt_price <= 0:
                continue

            # Max repay = 50% of borrow
            repay_raw = int(borrow_raw * _CLOSE_FACTOR)
            repay_usd = (repay_raw / 10 ** debt_dec) * debt_price

            for c_col in self._markets:
                if c_col == c_debt:
                    continue
                meta_col = self._market_meta.get(c_col)
                if not meta_col:
                    continue
                try:
                    ct_col   = self.w3.eth.contract(address=c_col, abi=_CTOKEN_ABI)
                    col_ctok = ct_col.functions.balanceOf(borrower).call()
                    if col_ctok == 0:
                        continue

                    exch_rate = ct_col.functions.exchangeRateStored().call()
                except Exception:
                    continue

                col_dec   = meta_col["decimals"]
                col_price = self._price_usd(c_col, col_dec)
                if col_price <= 0:
                    continue

                # Collateral value: cToken balance × exchangeRate / 1e18 → underlying
                col_und_raw = col_ctok * exch_rate // (10 ** 18)
                col_usd     = (col_und_raw / 10 ** col_dec) * col_price

                # Seized collateral = repay × liqIncentive (in USD)
                seized_usd = repay_usd * _LIQ_INCENTIVE

                # Can't seize more than borrower has
                if seized_usd > col_usd:
                    # Scale down repay to fit available collateral
                    scale      = col_usd / seized_usd
                    repay_raw  = int(repay_raw * scale)
                    repay_usd  = repay_usd * scale
                    seized_usd = repay_usd * _LIQ_INCENTIVE

                # Net profit estimate: bonus − Aave flash fee (0.05%) − gas
                flash_fee  = repay_usd * 0.0005
                gas_est    = 0.15   # ~$0.15 on Base
                net_profit = (seized_usd - repay_usd) - flash_fee - gas_est

                if net_profit < self.min_profit:
                    continue

                # Pool fee for swap
                col_und_lower = meta_col["underlying"].lower()
                pool_fee = _POOL_FEE.get(col_und_lower, _DEFAULT_POOL_FEE)

                opp = MoonwellOpportunity(
                    borrower        = borrower,
                    c_debt          = c_debt,
                    c_collateral    = c_col,
                    debt_underlying = meta_debt["underlying"],
                    col_underlying  = meta_col["underlying"],
                    debt_symbol     = meta_debt["und_symbol"],
                    col_symbol      = meta_col["und_symbol"],
                    repay_amount    = repay_raw,
                    repay_usd       = repay_usd,
                    col_seized_usd  = seized_usd,
                    net_profit_usd  = net_profit,
                    pool_fee        = pool_fee,
                )
                if best is None or opp.net_profit_usd > best.net_profit_usd:
                    best = opp

        return best

    # ── Dynamic gas pricing ───────────────────────────────────────────────────
    def _calc_gas_price(self, net_profit_usd: float) -> int:
        """Priority tip em 3 tiers baseado no lucro estimado. Devolve gasPrice em Wei."""
        try:
            base_fee = self.w3.eth.get_block("latest")["baseFeePerGas"]
        except Exception:
            base_fee = int(0.005 * 1e9)  # 0.005 gwei fallback (mínimo Base)

        if net_profit_usd < 50:
            tip_gwei = 1.5    # 1-2 gwei — baixa competição
        elif net_profit_usd < 500:
            tip_gwei = 7.5    # 5-10 gwei — competição média
        else:
            tip_gwei = 25.0   # 20-30 gwei — alta competição

        gas_price = base_fee + int(tip_gwei * 1e9)
        logger.info(
            "MoonwellBase: gas_price=%.4f gwei (base=%.4f + tip=%.1f) lucro≈$%.2f",
            gas_price / 1e9, base_fee / 1e9, tip_gwei, net_profit_usd,
        )
        return gas_price

    # ── Execute liquidation ───────────────────────────────────────────────────
    def _execute(self, opp: MoonwellOpportunity) -> bool:
        if self.flash is None:
            logger.error("MoonwellBase: sem contrato flash — não pode executar")
            return False

        wallet = get_env("WALLET_ADDRESS") or self.w3.eth.accounts[0] if self.w3.eth.accounts else None
        if not wallet:
            try:
                from eth_account import Account
                pk = get_env("BSC_PRIVATE_KEY", "").strip().strip('"').strip("'")
                if pk.startswith("0x"):
                    pk = pk[2:]
                wallet = Account.from_key(pk).address
            except Exception as e:
                logger.error("MoonwellBase: não conseguiu determinar wallet: %s", e)
                return False

        # ETH balance check
        eth_bal = self.w3.eth.get_balance(Web3.to_checksum_address(wallet)) / 1e18
        if eth_bal < 0.005:
            logger.error("MoonwellBase: saldo insuficiente (%.6f ETH < 0.005)", eth_bal)
            return False

        try:
            tx = self.flash.functions.executeMoonwellLiquidation(
                opp.c_debt,
                opp.c_collateral,
                opp.borrower,
                opp.repay_amount,
                opp.pool_fee,
            ).build_transaction({
                "from":     Web3.to_checksum_address(wallet),
                "gas":      800_000,
                "gasPrice": self._calc_gas_price(opp.net_profit_usd),
                "nonce":    self.w3.eth.get_transaction_count(
                    Web3.to_checksum_address(wallet)),
            })

            from eth_account import Account
            pk = get_env("BSC_PRIVATE_KEY", "").strip().strip('"').strip("'")
            if pk.startswith("0x"):
                pk = pk[2:]
            signed = Account.sign_transaction(tx, pk)
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

            if receipt.status == 1:
                logger.info("MoonwellBase: TX ✓ %s", tx_hash.hex())
                self.notifier.notify(
                    "liquidation",
                    f"🟢 MOONWELL BASE liquidação executada\n"
                    f"borrower={opp.borrower[:12]}…\n"
                    f"repay={opp.repay_usd:.2f} {opp.debt_symbol}\n"
                    f"lucro≈${opp.net_profit_usd:.2f}\n"
                    f"tx={tx_hash.hex()[:16]}…",
                )
                return True
            else:
                logger.error("MoonwellBase: TX reverteu %s", tx_hash.hex())
                return False

        except ContractLogicError as e:
            logger.error("MoonwellBase: ContractLogicError: %s", e)
            return False
        except Exception as e:
            logger.error("MoonwellBase: execução falhou: %s", e)
            return False

    # ── WebSocket listener ────────────────────────────────────────────────────
    def _ws_runner(self) -> None:
        """Corre o event-loop asyncio num daemon thread dedicado."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._ws_listen())
        finally:
            loop.close()

    async def _ws_listen(self) -> None:
        """Subscrita a newHeads no Base; reconecta com failover de URL."""
        wss_urls = [_BASE_WSS_PRIMARY, _BASE_WSS_FALLBACK]
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
                    logger.info(
                        "MoonwellBase: WS newHeads subscrito @ %s (id=%s)",
                        url.split("//")[-1].split("/")[0],
                        sub.get("result", "?")[:12],
                    )
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
                                    self._block_queue.get_nowait()  # descarta o mais antigo
                                    self._block_queue.put_nowait(blk)
                        except asyncio.TimeoutError:
                            continue   # keepalive — verifica _ws_stop
                        except Exception:
                            break      # ligação perdida — reconecta
            except Exception as exc:
                logger.warning(
                    "MoonwellBase: WS erro @ %s — reconecta em 5s: %s",
                    url.split("//")[-1].split("/")[0], exc,
                )
                idx += 1
            await asyncio.sleep(5)

    # ── Main tick ─────────────────────────────────────────────────────────────
    def tick(self) -> None:
        # ── Drena a fila — fica com o bloco mais recente ──────────────────────
        block_num = None
        try:
            while True:
                block_num = self._block_queue.get_nowait()
        except queue.Empty:
            pass

        # Fallback HTTP se WebSocket silencioso há >30s
        if block_num is None:
            if time.time() - self._ws_last_seen > 30:
                try:
                    block_num = self.w3.eth.block_number
                except Exception:
                    return
            else:
                return  # aguarda próximo bloco WS

        if block_num <= self._last_block:
            return
        self._last_block = block_num

        # ── Housekeeping ──────────────────────────────────────────────────────
        now = time.time()
        self._blacklist = {k: v for k, v in self._blacklist.items() if v > now}
        self._cooldown  = {k: v for k, v in self._cooldown.items()  if v > now}

        # ── Scan incremental: apenas a cada _SCAN_INTERVAL_BLOCKS (~2 min) ───
        if block_num % _SCAN_INTERVAL_BLOCKS == 0 or not self._borrowers:
            self._scan_borrowers()

        if not self._borrowers:
            logger.info("MoonwellBase: bloco %d — scan em progresso (%d mutuários)",
                        block_num, len(self._borrowers))
            return

        # Check subset of borrowers each tick
        candidates = [b for b in self._borrowers
                      if b not in self._blacklist and b not in self._cooldown]
        to_check   = candidates[:self.max_per_tick]

        liquidatable = 0
        executed     = 0

        for borrower in to_check:
            opp = self._check_borrower(borrower)
            if opp is None:
                continue

            liquidatable += 1
            logger.info(
                "MoonwellBase: LIQUIDAÇÃO %s  debt=%s $%.2f  col=%s  lucro≈$%.2f  dry=%s",
                borrower[:12], opp.debt_symbol, opp.repay_usd,
                opp.col_symbol, opp.net_profit_usd, self.dry_run,
            )

            upsert_liquidation_opportunity(
                protocol="moonwell_base",
                chain_id=8453,
                borrower=borrower,
                debt_asset=opp.debt_underlying,
                collateral_asset=opp.col_underlying,
                debt_to_cover=opp.repay_amount,
                collateral_amount=int(opp.col_seized_usd * 1e6),
                health_factor=0.99,  # shortfall > 0 implica HF < 1.0
                liquidation_bonus_pct=round((_LIQ_INCENTIVE - 1) * 100, 1),
                net_profit_usd=opp.net_profit_usd,
                dry_run=self.dry_run,
                status="dry_run" if self.dry_run else "pending",
            )

            if not self.dry_run:
                ok = self._execute(opp)
                if ok:
                    executed += 1
                    self._cooldown[borrower] = now + 300  # 5min cooldown
                    self._fail_counts.pop(borrower, None)
                else:
                    self._fail_counts[borrower] = self._fail_counts.get(borrower, 0) + 1
                    if self._fail_counts[borrower] >= 3:
                        self._blacklist[borrower] = now + 3600
                        logger.warning("MoonwellBase: %s blacklist 1h (3 falhas)", borrower[:12])

        logger.info(
            "MoonwellBase: tick — %d liquidáveis / %d executados (%d checados, %d mutuários)",
            liquidatable, executed, len(to_check), len(self._borrowers),
        )


if __name__ == "__main__":
    import json
    from utils.config import get_settings
    from utils.logger import setup_logging
    setup_logging()
    settings = get_settings()
    bot = MoonwellLiquidatorBaseBot(settings)
    print("=== MoonwellLiquidatorBaseBot — diagnóstico ===")
    bot._load_markets()
    print(f"Mercados: {len(bot._markets)}")
    print(f"dry_run : {bot.dry_run}")
    print("A scannar borrowers (últimos 50k blocos para teste)...")
    bot.scan_blocks = 50_000
    bot._scan_borrowers()
    print(f"Mutuários descobertos: {len(bot._borrowers)}")
    found = 0
    for b in list(bot._borrowers)[:200]:
        opp = bot._check_borrower(b)
        if opp:
            found += 1
            print(f"  {b[:12]}…  debt={opp.debt_symbol} ${opp.repay_usd:.2f}"
                  f"  col={opp.col_symbol}  lucro≈${opp.net_profit_usd:.2f}")
    print(f"\nTotal liquidáveis: {found} (dry_run={bot.dry_run})")
