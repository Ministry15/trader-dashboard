"""
ionic_liquidator_base_bot.py — Liquidações Ionic Protocol (Compound V2) em Base.

Ionic é um fork Compound V2 em Base (chain 8453).
Segue exatamente o padrão do moonwell_liquidator_base_bot.py:
  1. Scan eventos Borrow em cada ionToken → set de mutuários
  2. Por tick: getAccountLiquidity(borrower) → shortfall > 0 = liquidável
  3. Encontra o melhor par dívida/colateral
  4. Executa via flash loan (mesmo contrato do Moonwell: MoonwellLiquidatorBase.sol
     chama liquidateBorrow() genericamente — compatível com qualquer Compound V2)

Diferenças face ao moonwell_liquidator_base_bot.py:
  - Comptroller: 0x05c9C6417F246600f8f5f49fcA9Ee991bfF73D13
  - Oracle: descoberto dinamicamente via Comptroller.oracle()
  - LIQ_INCENTIVE: 8% (Ionic usa menor incentivo do que Moonwell)
  - Nome de protocolo na BD: "ionic_base"
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass
from typing import Optional

import websockets
from eth_account import Account
from web3 import Web3
from web3.exceptions import ContractLogicError

from utils.config import get_env, get_settings
from utils.database import init_db, upsert_liquidation_opportunity
from utils.flashbots import send_bundle as _fb_send_bundle
from utils.flashbots import send_bundle_multi as _fb_send_multi
from utils.logger import get_logger
from utils.notifier import TelegramNotifier

logger = get_logger("ionic_liquidator_base_bot")

# ── Log file ──────────────────────────────────────────────────────────────────
_LOG_DIR = "/opt/crypto_bsc/logs"
os.makedirs(_LOG_DIR, exist_ok=True)
_fh = logging.FileHandler(os.path.join(_LOG_DIR, "ionic_base.log"))
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s",
                                   datefmt="%Y-%m-%d %H:%M:%S"))
logger.addHandler(_fh)

# ── RPC (Base mainnet) ────────────────────────────────────────────────────────
_BASE_RPC_PRIMARY  = "https://base.publicnode.com"
_BASE_RPC_FALLBACK = "https://mainnet.base.org"
_BASE_WSS_PRIMARY  = "wss://base.publicnode.com"
_BASE_WSS_FALLBACK = "wss://base.drpc.org"

_SCAN_INTERVAL_BLOCKS  = 60   # ~2 min on Base (2s/block)

_FLASHBOTS_ENDPOINT       = "https://relay.flashbots.net"
_FLASHBOTS_MIN_PROFIT_USD = 500.0
_MAX_BUNDLE_TXS           = 4

# ── Ionic Base addresses ──────────────────────────────────────────────────────
_COMPTROLLER = Web3.to_checksum_address("0x05c9C6417F246600f8f5f49fcA9Ee991bfF73D13")

_LIQ_INCENTIVE = 1.08   # 8% typical Ionic liquidation bonus
_CLOSE_FACTOR  = 0.50   # max 50% of borrow repayable per liquidation

# Uniswap V3 pool fees for collateral → debt swap (same as Moonwell, same chain)
_POOL_FEE: dict[str, int] = {
    "0x4200000000000000000000000000000000000006": 500,   # WETH
    "0x2ae3f1ec7f1f5012cfeab0185bfc7aa3cf0dec22": 500,   # cbETH
    "0xc1cba3fcea344f92d9239c08c0568f6f2f0ee452": 500,   # wstETH
    "0x04c0599ae5a44757c0af6f9ec3b93da8976c150a": 500,   # weETH
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": 100,   # USDC native
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca": 100,   # USDbC
    "0x50c5725949a6f0c72e6c4a641f24049a917db0cb": 100,   # DAI
}
_DEFAULT_POOL_FEE = 3000

_BORROW_TOPIC = "0x" + Web3.keccak(text="Borrow(address,uint256,uint256,uint256)").hex()

# ── ABIs ──────────────────────────────────────────────────────────────────────
_COMPTROLLER_ABI = [
    {"inputs": [], "name": "getAllMarkets",
     "outputs": [{"type": "address[]"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"type": "address"}], "name": "getAccountLiquidity",
     "outputs": [{"type": "uint256"}, {"type": "uint256"}, {"type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "oracle",
     "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
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

# Reuses MoonwellLiquidatorBase.sol — same Compound V2 liquidateBorrow interface
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
class IonicOpportunity:
    borrower:        str
    c_debt:          str
    c_collateral:    str
    debt_underlying: str
    col_underlying:  str
    debt_symbol:     str
    col_symbol:      str
    repay_amount:    int
    repay_usd:       float
    col_seized_usd:  float
    net_profit_usd:  float
    pool_fee:        int


class IonicLiquidatorBaseBot:

    def __init__(self, settings: dict):
        self.settings = settings
        self.cfg = settings.get("bots", {}).get("ionic_liquidator_base", {})

        primary_rpc = get_env("ALCHEMY_BASE_URL") or _BASE_RPC_PRIMARY
        self._rpc_urls   = [primary_rpc, _BASE_RPC_PRIMARY, _BASE_RPC_FALLBACK]
        self._active_rpc = primary_rpc

        self.min_profit   = float(self.cfg.get("min_profit_usd", 10.0))
        self.scan_blocks  = int(self.cfg.get("borrower_scan_blocks", 500_000))
        self.max_per_tick = int(self.cfg.get("max_positions_per_tick", 100))

        flash_addr = (
            get_env("FLASH_LOAN_CONTRACT_BASE")
            or self.cfg.get("flash_loan_contract", "")
        )
        self.dry_run: bool = not bool(flash_addr)
        if self.dry_run:
            logger.warning("IonicBase: flash_loan_contract não configurado — DRY_RUN forçado")

        self.w3          = Web3(Web3.HTTPProvider(primary_rpc, request_kwargs={"timeout": 30}))
        self.comptroller = self.w3.eth.contract(address=_COMPTROLLER, abi=_COMPTROLLER_ABI)
        self.oracle      = None   # lazy-loaded in _load_markets
        self.flash       = (
            self.w3.eth.contract(
                address=Web3.to_checksum_address(flash_addr), abi=_FLASH_ABI
            ) if flash_addr else None
        )

        self._markets:     list[str] = []
        self._borrowers:   set[str]  = set()
        self._scan_from:   int       = 0
        self._market_meta: dict[str, dict] = {}

        self._cooldown:    dict[str, float] = {}
        self._blacklist:   dict[str, float] = {}
        self._fail_counts: dict[str, int]   = {}

        self._block_queue:  queue.Queue = queue.Queue(maxsize=20)
        self._last_block:   int   = 0
        self._ws_last_seen: float = time.time()
        self._ws_stop       = threading.Event()
        self._ws_thread     = threading.Thread(
            target=self._ws_runner, daemon=True, name="ionic-base-ws")
        self._ws_thread.start()

        self.notifier = TelegramNotifier(settings)
        init_db()

        logger.info(
            "IonicBase: rpc=%s dry_run=%s min_profit=$%.2f scan_blocks=%d",
            primary_rpc.split("//")[-1].split("/")[0],
            self.dry_run, self.min_profit, self.scan_blocks,
        )

    # ── RPC failover ──────────────────────────────────────────────────────────
    def _switch_rpc(self, failed: str) -> bool:
        for url in self._rpc_urls:
            if url != failed:
                self.w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 30}))
                self.comptroller = self.w3.eth.contract(address=_COMPTROLLER, abi=_COMPTROLLER_ABI)
                if self.oracle:
                    self.oracle = self.w3.eth.contract(address=self.oracle.address, abi=_ORACLE_ABI)
                if self.flash:
                    self.flash = self.w3.eth.contract(address=self.flash.address, abi=_FLASH_ABI)
                self._active_rpc = url
                logger.warning("IonicBase: RPC → %s", url.split("//")[-1].split("/")[0])
                return True
        return False

    # ── Market metadata ───────────────────────────────────────────────────────
    def _load_markets(self) -> None:
        if self._markets:
            return
        try:
            self._markets = [
                Web3.to_checksum_address(m)
                for m in self.comptroller.functions.getAllMarkets().call()
            ]
        except Exception as exc:
            logger.warning("IonicBase: getAllMarkets falhou: %s", exc)
            self._switch_rpc(self._active_rpc)
            return

        # Discover oracle from Comptroller
        if self.oracle is None:
            try:
                oracle_addr = self.comptroller.functions.oracle().call()
                self.oracle = self.w3.eth.contract(
                    address=Web3.to_checksum_address(oracle_addr), abi=_ORACLE_ABI)
                logger.info("IonicBase: oracle=%s", oracle_addr[:12] + "…")
            except Exception as exc:
                logger.warning("IonicBase: oracle() falhou: %s", exc)

        for mkt in self._markets:
            try:
                ct       = self.w3.eth.contract(address=mkt, abi=_CTOKEN_ABI)
                sym      = ct.functions.symbol().call()
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
            except Exception as exc:
                logger.debug("IonicBase: meta erro %s: %s", mkt[:10], exc)

        logger.info("IonicBase: %d mercados carregados (%d com meta)",
                    len(self._markets), len(self._market_meta))

    # ── Oracle price ──────────────────────────────────────────────────────────
    def _price_usd(self, ctoken: str, decimals: int) -> float:
        if self.oracle is None:
            return 0.0
        try:
            raw = self.oracle.functions.getUnderlyingPrice(
                Web3.to_checksum_address(ctoken)
            ).call()
            return raw / (10 ** (36 - decimals))
        except Exception:
            return 0.0

    # ── Scan Borrow events ────────────────────────────────────────────────────
    def _scan_borrowers(self) -> None:
        self._load_markets()
        if not self._markets:
            return
        try:
            latest = self.w3.eth.block_number
        except Exception:
            return

        if self._scan_from == 0:
            self._scan_from = max(0, latest - self.scan_blocks)

        scan_end  = min(self._scan_from + 20_000 - 1, latest)
        new_count = 0

        for mkt in self._markets:
            cur = self._scan_from
            while cur <= scan_end:
                end = min(cur + 2000 - 1, scan_end)
                try:
                    logs = self.w3.eth.get_logs({
                        "fromBlock": cur, "toBlock": end,
                        "address":   mkt, "topics": [_BORROW_TOPIC],
                    })
                    for log in logs:
                        raw = bytes(log["data"])
                        if len(raw) >= 32:
                            borrower = Web3.to_checksum_address("0x" + raw[:32].hex()[-40:])
                            if borrower not in self._borrowers:
                                self._borrowers.add(borrower)
                                new_count += 1
                except Exception:
                    pass
                cur = end + 1

        pct = min(100, int((scan_end - (latest - self.scan_blocks)) / self.scan_blocks * 100))
        self._scan_from = scan_end + 1
        if new_count or pct % 20 == 0:
            logger.info("IonicBase: scan %d%% | +%d mutuários (total=%d)",
                        pct, new_count, len(self._borrowers))

    # ── Check single borrower ─────────────────────────────────────────────────
    def _check_borrower(self, borrower: str) -> Optional[IonicOpportunity]:
        try:
            err, _, shortfall = self.comptroller.functions.getAccountLiquidity(borrower).call()
            if err != 0 or shortfall == 0:
                return None
        except Exception:
            return None

        best: Optional[IonicOpportunity] = None
        for c_debt in self._markets:
            meta_debt = self._market_meta.get(c_debt)
            if not meta_debt:
                continue
            try:
                borrow_raw = self.w3.eth.contract(address=c_debt, abi=_CTOKEN_ABI)\
                                 .functions.borrowBalanceStored(borrower).call()
                if borrow_raw == 0:
                    continue
            except Exception:
                continue

            debt_dec   = meta_debt["decimals"]
            debt_price = self._price_usd(c_debt, debt_dec)
            if debt_price <= 0:
                continue

            repay_raw = int(borrow_raw * _CLOSE_FACTOR)
            repay_usd = (repay_raw / 10 ** debt_dec) * debt_price

            for c_col in self._markets:
                if c_col == c_debt:
                    continue
                meta_col = self._market_meta.get(c_col)
                if not meta_col:
                    continue
                try:
                    ct_col    = self.w3.eth.contract(address=c_col, abi=_CTOKEN_ABI)
                    col_ctok  = ct_col.functions.balanceOf(borrower).call()
                    if col_ctok == 0:
                        continue
                    exch_rate = ct_col.functions.exchangeRateStored().call()
                except Exception:
                    continue

                col_dec   = meta_col["decimals"]
                col_price = self._price_usd(c_col, col_dec)
                if col_price <= 0:
                    continue

                col_und_raw = col_ctok * exch_rate // (10 ** 18)
                col_usd     = (col_und_raw / 10 ** col_dec) * col_price

                seized_usd = repay_usd * _LIQ_INCENTIVE
                if seized_usd > col_usd:
                    scale      = col_usd / seized_usd
                    repay_raw  = int(repay_raw * scale)
                    repay_usd  = repay_usd * scale
                    seized_usd = repay_usd * _LIQ_INCENTIVE

                flash_fee  = repay_usd * 0.0005   # Aave V3 0.05%
                gas_est    = 0.18                  # ~$0.18 on Base
                net_profit = (seized_usd - repay_usd) - flash_fee - gas_est

                if net_profit < self.min_profit:
                    continue

                pool_fee = _POOL_FEE.get(meta_col["underlying"].lower(), _DEFAULT_POOL_FEE)
                opp = IonicOpportunity(
                    borrower=borrower, c_debt=c_debt, c_collateral=c_col,
                    debt_underlying=meta_debt["underlying"],
                    col_underlying=meta_col["underlying"],
                    debt_symbol=meta_debt["und_symbol"], col_symbol=meta_col["und_symbol"],
                    repay_amount=repay_raw, repay_usd=repay_usd,
                    col_seized_usd=seized_usd, net_profit_usd=net_profit, pool_fee=pool_fee,
                )
                if best is None or opp.net_profit_usd > best.net_profit_usd:
                    best = opp

        return best

    # ── Dynamic gas pricing ───────────────────────────────────────────────────
    def _calc_gas_price(self, net_profit_usd: float) -> int:
        try:
            base_fee = self.w3.eth.get_block("latest")["baseFeePerGas"]
        except Exception:
            base_fee = int(0.005 * 1e9)

        if net_profit_usd < 50:
            tip_gwei = 1.5
        elif net_profit_usd < 500:
            tip_gwei = 7.5
        else:
            tip_gwei = 25.0

        return base_fee + int(tip_gwei * 1e9)

    # ── Execute liquidation ───────────────────────────────────────────────────
    def _execute(self, opp: IonicOpportunity) -> bool:
        if self.flash is None:
            return False
        try:
            pk = get_env("BSC_PRIVATE_KEY", "").strip().strip('"').strip("'")
            if pk.startswith("0x"):
                pk = pk[2:]
            acct = Account.from_key(pk)

            eth_bal = self.w3.eth.get_balance(acct.address) / 1e18
            if eth_bal < 0.005:
                logger.error("IonicBase: saldo insuficiente (%.6f ETH < 0.005)", eth_bal)
                return False

            tx = self.flash.functions.executeMoonwellLiquidation(
                opp.c_debt, opp.c_collateral, opp.borrower,
                opp.repay_amount, opp.pool_fee,
            ).build_transaction({
                "from":     acct.address,
                "gas":      900_000,
                "gasPrice": self._calc_gas_price(opp.net_profit_usd),
                "nonce":    self.w3.eth.get_transaction_count(acct.address),
                "chainId":  8453,
            })

            signed = Account.sign_transaction(tx, pk)

            if opp.net_profit_usd >= _FLASHBOTS_MIN_PROFIT_USD:
                try:
                    _tgt = self.w3.eth.block_number + 1
                    _bh  = _fb_send_bundle(
                        "0x" + signed.raw_transaction.hex(),
                        _tgt, _FLASHBOTS_ENDPOINT, pk,
                    )
                    if _bh:
                        _exp = Web3.keccak(primitive=bytes(signed.raw_transaction))
                        logger.info("IonicBase: TX via Flashbots @ bloco %d: %s…", _tgt, _exp.hex()[:18])
                        self.notifier.notify("liquidation",
                            f"🔵 IONIC BASE via Flashbots bloco {_tgt}\n"
                            f"borrower={opp.borrower[:12]}…\n"
                            f"lucro≈${opp.net_profit_usd:.2f}\n"
                            f"tx≈{_exp.hex()[:16]}…")
                        return True
                except Exception as fb_exc:
                    logger.warning("IonicBase: Flashbots falhou — fallback mempool: %s", fb_exc)

            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            if receipt.status == 1:
                logger.info("IonicBase: TX ✓ %s", tx_hash.hex())
                self.notifier.notify("liquidation",
                    f"🔵 IONIC BASE liquidação executada\n"
                    f"borrower={opp.borrower[:12]}…\n"
                    f"repay=${opp.repay_usd:.2f} {opp.debt_symbol}\n"
                    f"lucro≈${opp.net_profit_usd:.2f}\n"
                    f"tx={tx_hash.hex()[:16]}…")
                return True
            else:
                logger.error("IonicBase: TX reverteu %s", tx_hash.hex())
                return False

        except ContractLogicError as exc:
            logger.error("IonicBase: ContractLogicError: %s", exc)
            return False
        except Exception as exc:
            logger.error("IonicBase: execução falhou: %s", exc)
            return False

    # ── Multi-bundle (Phase 6) ───────────────────────────────────────────────
    def _try_bundle(self, opps: list) -> set[str]:
        """Same flash contract as Moonwell — bundle N executeMoonwellLiquidation calls."""
        if self.flash is None or len(opps) < 2:
            return set()
        try:
            from eth_account import Account as _Acct
            pk = get_env("BSC_PRIVATE_KEY", "").strip().strip('"').strip("'")
            if pk.startswith("0x"):
                pk = pk[2:]
            acct       = _Acct.from_key(pk)
            gas_price  = self._calc_gas_price(max(o.net_profit_usd for o in opps))
            base_nonce = self.w3.eth.get_transaction_count(acct.address)
            target     = self.w3.eth.block_number + 1

            raw_txes, borrowers = [], []
            for i, opp in enumerate(opps[:_MAX_BUNDLE_TXS]):
                tx = self.flash.functions.executeMoonwellLiquidation(
                    opp.c_debt, opp.c_collateral, opp.borrower,
                    opp.repay_amount, opp.pool_fee,
                ).build_transaction({
                    "from":     acct.address,
                    "gas":      900_000,
                    "gasPrice": gas_price,
                    "nonce":    base_nonce + i,
                    "chainId":  8453,
                })
                signed = _Acct.sign_transaction(tx, pk)
                raw_txes.append("0x" + signed.raw_transaction.hex())
                borrowers.append(opp.borrower)

            bh = _fb_send_multi(raw_txes, target, _FLASHBOTS_ENDPOINT, pk)
            if bh:
                logger.info("IonicBase: bundle %d txs @ bloco %d: %s…",
                            len(raw_txes), target, bh[:16])
                return set(borrowers)
            return set()
        except Exception as exc:
            logger.warning("IonicBase: bundle falhou → fallback individual: %s", exc)
            return set()

    # ── WebSocket ─────────────────────────────────────────────────────────────
    def _ws_runner(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._ws_listen())
        finally:
            loop.close()

    async def _ws_listen(self) -> None:
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
                    logger.info("IonicBase: WS newHeads subscrito @ %s (id=%s)",
                                url.split("//")[-1].split("/")[0],
                                sub.get("result", "?")[:12])
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
                logger.warning("IonicBase: WS erro @ %s — reconecta em 5s: %s",
                               url.split("//")[-1].split("/")[0], exc)
                idx += 1
            await asyncio.sleep(5)

    # ── Main tick ─────────────────────────────────────────────────────────────
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

        now = time.time()
        self._blacklist = {k: v for k, v in self._blacklist.items() if v > now}
        self._cooldown  = {k: v for k, v in self._cooldown.items()  if v > now}

        if block_num % _SCAN_INTERVAL_BLOCKS == 0 or not self._borrowers:
            self._scan_borrowers()

        if not self._borrowers:
            return []

        candidates = [b for b in self._borrowers
                      if b not in self._blacklist and b not in self._cooldown]
        to_check = candidates[:self.max_per_tick]

        results: list[dict] = []
        liquidatable = executed = 0

        # ── Pass 1: verificar todos ───────────────────────────────────────────
        checked_opps: list[tuple] = []
        for borrower in to_check:
            opp = self._check_borrower(borrower)
            if opp is not None:
                checked_opps.append((opp, borrower))

        # ── Bundle attempt (Phase 6) ──────────────────────────────────────────
        bundled: set[str] = set()
        if not self.dry_run and len(checked_opps) >= 2:
            _bdl = sorted(
                [(o, b) for o, b in checked_opps if o.net_profit_usd >= self.min_profit],
                key=lambda x: -x[0].net_profit_usd,
            )
            if len(_bdl) >= 2:
                bundled = self._try_bundle([o for o, _ in _bdl])
                if bundled:
                    for _, b in _bdl:
                        if b in bundled:
                            executed += 1
                            self._cooldown[b] = now + 300
                            self._fail_counts.pop(b, None)
                    logger.info("IonicBase: %d txs enviadas via bundle Flashbots", len(bundled))

        # ── Pass 2: log, upsert, execução individual ──────────────────────────
        for opp, borrower in checked_opps:
            liquidatable += 1
            logger.info(
                "IonicBase: LIQUIDAÇÃO %s  debt=%s $%.2f  col=%s  lucro≈$%.2f  dry=%s%s",
                borrower[:12], opp.debt_symbol, opp.repay_usd,
                opp.col_symbol, opp.net_profit_usd, self.dry_run,
                " [bundled]" if borrower in bundled else "",
            )

            upsert_liquidation_opportunity(
                protocol="ionic_base",
                chain_id=8453,
                borrower=borrower,
                debt_asset=opp.debt_underlying,
                collateral_asset=opp.col_underlying,
                debt_to_cover=opp.repay_amount,
                collateral_amount=int(opp.col_seized_usd * 1e6),
                health_factor=0.99,
                liquidation_bonus_pct=round((_LIQ_INCENTIVE - 1) * 100, 1),
                net_profit_usd=opp.net_profit_usd,
                dry_run=self.dry_run,
                status="dry_run" if self.dry_run else ("bundled" if borrower in bundled else "pending"),
            )

            if borrower in bundled:
                results.append({"borrower": borrower, "debt_usd": opp.repay_usd,
                                 "profit_usd": opp.net_profit_usd, "executed": True,
                                 "dry_run": False})
                continue

            if not self.dry_run:
                ok = self._execute(opp)
                if ok:
                    executed += 1
                    self._cooldown[borrower] = now + 300
                    self._fail_counts.pop(borrower, None)
                else:
                    self._fail_counts[borrower] = self._fail_counts.get(borrower, 0) + 1
                    if self._fail_counts[borrower] >= 3:
                        self._blacklist[borrower] = now + 3600
                        logger.warning("IonicBase: %s blacklist 1h (3 falhas)", borrower[:12])

            results.append({
                "borrower":    borrower,
                "debt_usd":    opp.repay_usd,
                "profit_usd":  opp.net_profit_usd,
                "executed":    executed > 0,
                "dry_run":     self.dry_run,
            })

        logger.info(
            "IonicBase: tick bloco=%d — %d liquidáveis / %d executados (%d bundled, %d checados)",
            block_num, liquidatable, executed, len(bundled), len(to_check),
        )
        return results
