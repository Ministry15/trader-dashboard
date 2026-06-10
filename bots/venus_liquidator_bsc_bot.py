"""
venus_liquidator_bsc_bot.py — Liquidações Venus Protocol (Compound V2) em BSC.

Venus é o maior fork Compound V2 em BSC (chain 56).
Segue o padrão do moonwell_liquidator_base_bot.py com as seguintes diferenças:
  - BSC RPC (publicnode) em vez de Base
  - chain_id=56, gas em BNB (~$0.01/tx)
  - Sem Flashbots (sequenciador BSC é descentralizado mas sem suporte Flashbots relevante)
  - Sem flash loan por enquanto (dry_run=True) — necessário contrato BSC separado
  - vBNB (0xA07c5b74C9B40447a954e1466938b865b6BBea36) não tem underlying() — tratado especialmente

Endereços principais Venus Core Pool:
  Comptroller: 0xfD36E2c2a6789Db23113685031d7F16329158384
  vBNB:        0xA07c5b74C9B40447a954e1466938b865b6BBea36
  vUSDT:       0xfD5840Cd36d94D7229439859C0112a4185BC0255
  vBTC:        0x882C173bC7Ff3b7786CA16DfeD3DFFfb9Ee7847B
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

from utils.config import get_env, get_settings
from utils.database import init_db, upsert_liquidation_opportunity
from utils.logger import get_logger
from utils.notifier import TelegramNotifier

logger = get_logger("venus_liquidator_bsc_bot")

# ── Log file ──────────────────────────────────────────────────────────────────
_LOG_DIR = "/opt/crypto_bsc/logs"
os.makedirs(_LOG_DIR, exist_ok=True)
_fh = logging.FileHandler(os.path.join(_LOG_DIR, "venus_bsc.log"))
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s",
                                   datefmt="%Y-%m-%d %H:%M:%S"))
logger.addHandler(_fh)

# ── RPC (BSC mainnet) ─────────────────────────────────────────────────────────
_BSC_RPC_PRIMARY  = "https://bsc.publicnode.com"
_BSC_RPC_FALLBACK = "https://bsc-dataseed.binance.org"
_BSC_WSS_PRIMARY  = "wss://bsc.publicnode.com"
_BSC_WSS_FALLBACK = "wss://bsc-rpc.publicnode.com"

_SCAN_INTERVAL_BLOCKS = 100   # ~5 min on BSC (3s/block)

# ── Venus Core Pool (BSC mainnet) ─────────────────────────────────────────────
_COMPTROLLER = Web3.to_checksum_address("0xfD36E2c2a6789Db23113685031d7F16329158384")

# vBNB: native token wrapper, no underlying() function
_VBNB_ADDR = Web3.to_checksum_address("0xA07c5b74C9B40447a954e1466938b865b6BBea36")
# BNB native pseudo-address (industry standard placeholder)
_BNB_UNDERLYING = Web3.to_checksum_address("0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE")
_BNB_DECIMALS   = 18
_BNB_SYMBOL     = "BNB"

_LIQ_INCENTIVE = 1.10   # Venus uses 10% liquidation incentive
_CLOSE_FACTOR  = 0.50   # max 50% of borrow per liquidation

# Gas estimate in USD (BSC: ~200k gas × 3 gwei × $600/BNB ≈ $0.36)
_GAS_EST_USD = 0.40

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

_VTOKEN_ABI = [
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


@dataclass
class VenusOpportunity:
    borrower:        str
    v_debt:          str
    v_collateral:    str
    debt_underlying: str
    col_underlying:  str
    debt_symbol:     str
    col_symbol:      str
    repay_amount:    int
    repay_usd:       float
    col_seized_usd:  float
    net_profit_usd:  float


class VenusLiquidatorBscBot:

    def __init__(self, settings: dict):
        self.settings = settings
        self.cfg = settings.get("bots", {}).get("venus_liquidator_bsc", {})

        primary_rpc = get_env("BSC_RPC_URL") or _BSC_RPC_PRIMARY
        self._rpc_urls   = [primary_rpc, _BSC_RPC_PRIMARY, _BSC_RPC_FALLBACK]
        self._active_rpc = primary_rpc

        self.min_profit   = float(self.cfg.get("min_profit_usd", 15.0))
        self.scan_blocks  = int(self.cfg.get("borrower_scan_blocks", 500_000))
        self.max_per_tick = int(self.cfg.get("max_positions_per_tick", 100))

        # Venus BSC: no flash loan contract deployed — always dry_run
        flash_addr = get_env("FLASH_LOAN_CONTRACT_BSC") or self.cfg.get("flash_loan_contract", "")
        self.dry_run: bool = not bool(flash_addr)
        if self.dry_run:
            logger.warning("VenusBSC: sem flash_loan_contract — DRY_RUN forçado")

        self.w3          = Web3(Web3.HTTPProvider(primary_rpc, request_kwargs={"timeout": 30}))
        self.comptroller = self.w3.eth.contract(address=_COMPTROLLER, abi=_COMPTROLLER_ABI)
        self.oracle      = None   # lazy-loaded in _load_markets

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
            target=self._ws_runner, daemon=True, name="venus-bsc-ws")
        self._ws_thread.start()

        self.notifier = TelegramNotifier(settings)
        init_db()

        logger.info(
            "VenusBSC: rpc=%s dry_run=%s min_profit=$%.2f scan_blocks=%d",
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
                self._active_rpc = url
                logger.warning("VenusBSC: RPC → %s", url.split("//")[-1].split("/")[0])
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
            logger.warning("VenusBSC: getAllMarkets falhou: %s", exc)
            return

        if self.oracle is None:
            try:
                oracle_addr = self.comptroller.functions.oracle().call()
                self.oracle = self.w3.eth.contract(
                    address=Web3.to_checksum_address(oracle_addr), abi=_ORACLE_ABI)
                logger.info("VenusBSC: oracle=%s", oracle_addr[:12] + "…")
            except Exception as exc:
                logger.warning("VenusBSC: oracle() falhou: %s", exc)

        for mkt in self._markets:
            try:
                vt  = self.w3.eth.contract(address=mkt, abi=_VTOKEN_ABI)
                sym = vt.functions.symbol().call()

                # vBNB has no underlying() — handle specially
                if mkt == _VBNB_ADDR:
                    self._market_meta[mkt] = {
                        "symbol":     sym,
                        "underlying": _BNB_UNDERLYING,
                        "decimals":   _BNB_DECIMALS,
                        "und_symbol": _BNB_SYMBOL,
                    }
                    continue

                try:
                    und_addr = Web3.to_checksum_address(vt.functions.underlying().call())
                    und_ct   = self.w3.eth.contract(address=und_addr, abi=_ERC20_ABI)
                    und_dec  = und_ct.functions.decimals().call()
                    und_sym  = und_ct.functions.symbol().call()
                except Exception:
                    # Skip markets where underlying() fails (native wrappers)
                    logger.debug("VenusBSC: underlying() falhou para %s — skip", sym)
                    continue

                self._market_meta[mkt] = {
                    "symbol":     sym,
                    "underlying": und_addr,
                    "decimals":   und_dec,
                    "und_symbol": und_sym,
                }
            except Exception as exc:
                logger.debug("VenusBSC: meta erro %s: %s", mkt[:10], exc)

        logger.info("VenusBSC: %d mercados carregados (%d com meta)",
                    len(self._markets), len(self._market_meta))

    # ── Oracle price ──────────────────────────────────────────────────────────
    def _price_usd(self, vtoken: str, decimals: int) -> float:
        if self.oracle is None:
            return 0.0
        try:
            raw = self.oracle.functions.getUnderlyingPrice(
                Web3.to_checksum_address(vtoken)
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
        if new_count or pct % 25 == 0:
            logger.info("VenusBSC: scan %d%% | +%d mutuários (total=%d)",
                        pct, new_count, len(self._borrowers))

    # ── Check single borrower ─────────────────────────────────────────────────
    def _check_borrower(self, borrower: str) -> Optional[VenusOpportunity]:
        try:
            err, _, shortfall = self.comptroller.functions.getAccountLiquidity(borrower).call()
            if err != 0 or shortfall == 0:
                return None
        except Exception:
            return None

        best: Optional[VenusOpportunity] = None
        for v_debt in self._markets:
            meta_debt = self._market_meta.get(v_debt)
            if not meta_debt:
                continue
            try:
                borrow_raw = self.w3.eth.contract(address=v_debt, abi=_VTOKEN_ABI)\
                                 .functions.borrowBalanceStored(borrower).call()
                if borrow_raw == 0:
                    continue
            except Exception:
                continue

            debt_dec   = meta_debt["decimals"]
            debt_price = self._price_usd(v_debt, debt_dec)
            if debt_price <= 0:
                continue

            repay_raw = int(borrow_raw * _CLOSE_FACTOR)
            repay_usd = (repay_raw / 10 ** debt_dec) * debt_price

            for v_col in self._markets:
                if v_col == v_debt:
                    continue
                meta_col = self._market_meta.get(v_col)
                if not meta_col:
                    continue
                try:
                    vt_col   = self.w3.eth.contract(address=v_col, abi=_VTOKEN_ABI)
                    col_vtok = vt_col.functions.balanceOf(borrower).call()
                    if col_vtok == 0:
                        continue
                    exch_rate = vt_col.functions.exchangeRateStored().call()
                except Exception:
                    continue

                col_dec   = meta_col["decimals"]
                col_price = self._price_usd(v_col, col_dec)
                if col_price <= 0:
                    continue

                col_und_raw = col_vtok * exch_rate // (10 ** 18)
                col_usd     = (col_und_raw / 10 ** col_dec) * col_price

                seized_usd = repay_usd * _LIQ_INCENTIVE
                if seized_usd > col_usd:
                    scale      = col_usd / seized_usd
                    repay_raw  = int(repay_raw * scale)
                    repay_usd  = repay_usd * scale
                    seized_usd = repay_usd * _LIQ_INCENTIVE

                # No flash loan → profit = incentive spread minus gas only
                net_profit = (seized_usd - repay_usd) - _GAS_EST_USD

                if net_profit < self.min_profit:
                    continue

                opp = VenusOpportunity(
                    borrower=borrower, v_debt=v_debt, v_collateral=v_col,
                    debt_underlying=meta_debt["underlying"],
                    col_underlying=meta_col["underlying"],
                    debt_symbol=meta_debt["und_symbol"], col_symbol=meta_col["und_symbol"],
                    repay_amount=repay_raw, repay_usd=repay_usd,
                    col_seized_usd=seized_usd, net_profit_usd=net_profit,
                )
                if best is None or opp.net_profit_usd > best.net_profit_usd:
                    best = opp

        return best

    # ── WebSocket ─────────────────────────────────────────────────────────────
    def _ws_runner(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._ws_listen())
        finally:
            loop.close()

    async def _ws_listen(self) -> None:
        wss_urls = [_BSC_WSS_PRIMARY, _BSC_WSS_FALLBACK]
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
                    logger.info("VenusBSC: WS newHeads subscrito @ %s (id=%s)",
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
                logger.warning("VenusBSC: WS erro @ %s — reconecta em 5s: %s",
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
        liquidatable = 0

        for borrower in to_check:
            opp = self._check_borrower(borrower)
            if opp is None:
                continue

            liquidatable += 1
            logger.info(
                "VenusBSC: LIQUIDÁVEL %s  debt=%s $%.2f  col=%s  lucro≈$%.2f  [dry_run]",
                borrower[:12], opp.debt_symbol, opp.repay_usd,
                opp.col_symbol, opp.net_profit_usd,
            )

            upsert_liquidation_opportunity(
                protocol="venus_bsc",
                chain_id=56,
                borrower=borrower,
                debt_asset=opp.debt_underlying,
                collateral_asset=opp.col_underlying,
                debt_to_cover=opp.repay_amount,
                collateral_amount=int(opp.col_seized_usd * 1e6),
                health_factor=0.99,
                liquidation_bonus_pct=round((_LIQ_INCENTIVE - 1) * 100, 1),
                net_profit_usd=opp.net_profit_usd,
                dry_run=True,
                status="dry_run",
            )

            self.notifier.notify("liquidation",
                f"🟡 VENUS BSC [DRY] {borrower[:12]}…\n"
                f"debt={opp.debt_symbol} ${opp.repay_usd:.2f}\n"
                f"col={opp.col_symbol}\n"
                f"lucro≈${opp.net_profit_usd:.2f}")

            results.append({
                "borrower":   borrower,
                "debt_usd":   opp.repay_usd,
                "profit_usd": opp.net_profit_usd,
                "executed":   False,
                "dry_run":    True,
            })

        logger.info(
            "VenusBSC: tick bloco=%d — %d liquidáveis (%d checados)",
            block_num, liquidatable, len(to_check),
        )
        return results
