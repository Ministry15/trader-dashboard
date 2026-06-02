"""Funding Rate Arbitrage + Grid CEX na Binance.

Duas sub-estratégias combinadas numa única classe orquestradora:

1. **FundingRateScanner** — monitoriza funding rates de 50 pares perpétuos
   USDT-M na Binance Futures (via ccxt.binanceusdm). Quando |funding| supera
   0.05%/8h, abre posição delta-neutral: se funding > 0 → short futuros + long
   spot (recebe o funding); se funding < 0 → long futuros + short spot.
   Relatório top-5 enviado ao Telegram a cada hora.

2. **CexGridBot** — grelha de ordens limite em DOGE/USDT na Binance Spot.
   Capital definido por ``GRID_CAPITAL_USDT`` no .env (default: 100 USDT).
   Range ±15% do preço actual, 20 níveis. Sem gas fees — CEX directo.

Ambas as estratégias respeitam ``DRY_RUN``: as ordens são construídas e
logadas mas NÃO enviadas enquanto ``DRY_RUN != false``.

Compatível com o BOT_REGISTRY de ``main.py`` via método ``tick()``.
Serviço standalone: ``scripts/tgbot-funding.service``.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

import ccxt

from utils.config import get_env, get_settings
from utils.database import init_db, record_trade
from utils.logger import get_logger
from utils.notifier import TelegramNotifier

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
FUNDING_THRESHOLD = 0.0005      # 0.05% por período de 8h
FUNDING_SCAN_SYMBOLS = 50       # top-N pares a monitorizar (por |funding rate|)
TOP_N_REPORT = 5                # pares no relatório Telegram

GRID_PAIR = "DOGE/USDT"
GRID_LEVELS = 20
GRID_RANGE_PCT = Decimal("0.15")  # ±15% do preço de entrada


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dry_run_flag() -> bool:
    return str(get_env("DRY_RUN", "true")).lower() not in ("false", "0", "no")


# ---------------------------------------------------------------------------
# FundingRateScanner
# ---------------------------------------------------------------------------

@dataclass
class FundingOpportunity:
    symbol: str          # ex: BTC/USDT:USDT
    funding_rate: float  # taxa do próximo período (ex: 0.0001 = 0.01%)
    mark_price: float

    @property
    def funding_pct(self) -> float:
        return self.funding_rate * 100

    def summary(self) -> str:
        action = "short fut + long spot" if self.funding_rate > 0 else "long fut + short spot"
        return f"{self.symbol}: {self.funding_pct:+.4f}% → {action}"


class FundingRateScanner:
    """Monitoriza funding rates e abre posições delta-neutral quando oportuno."""

    def __init__(self, futures_ex: ccxt.Exchange, spot_ex: ccxt.Exchange,
                 dry_run: bool, notifier: TelegramNotifier,
                 position_size_usdt: float):
        self.futures_ex = futures_ex
        self.spot_ex = spot_ex
        self.dry_run = dry_run
        self.notifier = notifier
        self.position_size_usdt = position_size_usdt
        self._last_report_ts: float = 0.0

    def _fetch_top_rates(self) -> list[FundingOpportunity]:
        try:
            all_rates = self.futures_ex.fetch_funding_rates()
        except Exception as exc:
            logger.warning("fetch_funding_rates falhou: %s", exc)
            return []

        opps: list[FundingOpportunity] = []
        for sym, data in all_rates.items():
            if not sym.endswith("/USDT:USDT"):
                continue
            rate = data.get("fundingRate")
            mark = data.get("markPrice") or 0.0
            if rate is None:
                continue
            try:
                opps.append(FundingOpportunity(
                    symbol=sym,
                    funding_rate=float(rate),
                    mark_price=float(mark),
                ))
            except (ValueError, TypeError):
                continue

        opps.sort(key=lambda o: abs(o.funding_rate), reverse=True)
        return opps[:FUNDING_SCAN_SYMBOLS]

    def _report_top5(self, opps: list[FundingOpportunity]) -> None:
        if not opps:
            return
        lines = [f"<b>Top {TOP_N_REPORT} Funding Rates (8h)</b>"]
        for o in opps[:TOP_N_REPORT]:
            label = o.symbol.replace("/USDT:USDT", "")
            lines.append(f"  • {label}: <code>{o.funding_pct:+.4f}%</code>"
                         f"  mark={o.mark_price:.4f}")
        self.notifier.send("\n".join(lines))
        logger.info("Relatório top-%d funding rates enviado.", TOP_N_REPORT)

    def _execute_delta_neutral(self, opp: FundingOpportunity) -> dict:
        base = opp.symbol.split("/")[0]
        spot_sym = f"{base}/USDT"
        fut_side = "sell" if opp.funding_rate > 0 else "buy"
        spot_side = "buy" if opp.funding_rate > 0 else "sell"
        qty = round(self.position_size_usdt / opp.mark_price, 4) if opp.mark_price else 0

        if self.dry_run:
            logger.info(
                "[DRY_RUN] Delta-neutral %s: fut %s %.4f | spot %s %.4f",
                opp.symbol, fut_side, qty, spot_side, qty,
            )
            return {"dry_run": True, "symbol": opp.symbol,
                    "fut_side": fut_side, "spot_side": spot_side, "qty": qty}

        results: dict = {}
        try:
            results["futures"] = self.futures_ex.create_market_order(
                opp.symbol, fut_side, qty)
        except Exception as exc:
            logger.error("Erro leg futures %s: %s", opp.symbol, exc)
            results["futures_error"] = str(exc)
        try:
            results["spot"] = self.spot_ex.create_market_order(
                spot_sym, spot_side, qty)
        except Exception as exc:
            logger.error("Erro leg spot %s: %s", spot_sym, exc)
            results["spot_error"] = str(exc)
        return results

    def scan_and_act(self) -> list[dict]:
        """Obtém funding rates, envia relatório (1×/hora), actua acima do threshold."""
        opps = self._fetch_top_rates()
        now = time.time()
        if now - self._last_report_ts >= 3600:
            self._report_top5(opps)
            self._last_report_ts = now
            logger.info(
                "Top-%d: %s",
                TOP_N_REPORT,
                [f"{o.symbol.split('/')[0]}={o.funding_pct:+.4f}%" for o in opps[:TOP_N_REPORT]],
            )

        results = []
        for opp in opps:
            if abs(opp.funding_rate) < FUNDING_THRESHOLD:
                break  # lista está ordenada por |rate| → o resto está abaixo
            logger.info("Funding opportunity: %s", opp.summary())
            res = self._execute_delta_neutral(opp)
            record_trade(
                bot="funding_rate",
                base=opp.symbol.split("/")[0],
                quote="USDT",
                size_usd=self.position_size_usdt,
                dry_run=self.dry_run,
                status="dry_run" if self.dry_run else "open",
            )
            self.notifier.notify(
                "trade_executed",
                f"Funding: {opp.summary()}\nsize={self.position_size_usdt:.0f}USDT"
                f"  dry_run={self.dry_run}",
            )
            results.append({"opp": opp, "result": res})
        return results


# ---------------------------------------------------------------------------
# CexGridBot
# ---------------------------------------------------------------------------

class CexGridBot:
    """Grelha de ordens limite em DOGE/USDT — puramente na Binance Spot."""

    def __init__(self, exchange: ccxt.Exchange, dry_run: bool,
                 capital_usdt: float, notifier: TelegramNotifier):
        self.ex = exchange
        self.dry_run = dry_run
        self.capital_usdt = capital_usdt
        self.notifier = notifier
        self.grid_prices: list[Decimal] = []
        self.order_size_usdt: Decimal = Decimal("0")
        self.prev_price: Optional[Decimal] = None

    def _fetch_price(self) -> Optional[Decimal]:
        try:
            ticker = self.ex.fetch_ticker(GRID_PAIR)
            p = ticker.get("last") or ticker.get("close")
            return Decimal(str(p)) if p else None
        except Exception as exc:
            logger.warning("fetch_ticker %s falhou: %s", GRID_PAIR, exc)
            return None

    def _build_grid(self, mid: Decimal) -> None:
        lower = mid * (1 - GRID_RANGE_PCT)
        upper = mid * (1 + GRID_RANGE_PCT)
        step = (upper - lower) / (GRID_LEVELS - 1)
        self.grid_prices = [lower + step * i for i in range(GRID_LEVELS)]
        self.order_size_usdt = Decimal(str(self.capital_usdt)) / GRID_LEVELS
        logger.info(
            "CexGrid DOGE/USDT: %d níveis [%.4f..%.4f], %.2f USDT/ordem, dry_run=%s",
            GRID_LEVELS, float(lower), float(upper),
            float(self.order_size_usdt), self.dry_run,
        )

    def _crossings(self, prev: Decimal, cur: Decimal) -> list[tuple[str, Decimal]]:
        out = []
        for g in self.grid_prices:
            if prev > g >= cur:     # desceu através de g → comprar
                out.append(("buy", g))
            elif prev < g <= cur:   # subiu através de g → vender
                out.append(("sell", g))
        return out

    def _place(self, side: str, level: Decimal, price: Decimal) -> dict:
        qty = float(self.order_size_usdt / price)
        qty = max(1.0, round(qty, 0))  # lote mínimo DOGE = 1

        if self.dry_run:
            logger.info(
                "[DRY_RUN] CEX GRID %s %.0f DOGE @ %.4f",
                side.upper(), qty, float(level),
            )
            return {"dry_run": True, "side": side, "qty": qty, "price": float(level)}

        try:
            order = self.ex.create_limit_order(GRID_PAIR, side, qty, float(level))
            logger.info("CEX GRID ordem %s id=%s", side, order.get("id"))
            return order
        except Exception as exc:
            logger.error("Erro ordem CEX grid: %s", exc)
            return {"error": str(exc)}

    def tick(self) -> list[dict]:
        price = self._fetch_price()
        if price is None:
            return []

        if not self.grid_prices:
            self._build_grid(price)
            self.prev_price = price
            return []

        if self.prev_price is None:
            self.prev_price = price
            return []

        signals = self._crossings(self.prev_price, price)
        self.prev_price = price
        results = []
        for side, level in signals:
            res = self._place(side, level, price)
            record_trade(
                bot="cex_grid",
                base="DOGE",
                quote="USDT",
                dex_buy="binance" if side == "buy" else None,
                dex_sell="binance" if side == "sell" else None,
                size_usd=float(self.order_size_usdt),
                dry_run=self.dry_run,
                status="dry_run" if self.dry_run else "sent",
            )
            self.notifier.notify(
                "trade_executed",
                f"CEX GRID {side.upper()} DOGE/USDT @ {float(level):.4f}"
                f"  dry_run={self.dry_run}",
            )
            results.append(res)
        return results


# ---------------------------------------------------------------------------
# FundingRateBot — fachada principal (compatível com BOT_REGISTRY do main.py)
# ---------------------------------------------------------------------------

class FundingRateBot:
    """Orquestra FundingRateScanner + CexGridBot. Interface: tick() / run_forever()."""

    def __init__(self, settings: dict | None = None):
        self.settings = settings or get_settings()
        self.dry_run = _dry_run_flag()
        self.notifier = TelegramNotifier(self.settings)
        init_db()

        api_key = get_env("BINANCE_API_KEY", "")
        api_secret = get_env("BINANCE_SECRET_KEY", "")
        capital = float(get_env("GRID_CAPITAL_USDT") or "100")

        creds: dict = {"apiKey": api_key, "secret": api_secret, "enableRateLimit": True}
        spot_ex = ccxt.binance({**creds, "options": {"defaultType": "spot"}})
        futures_ex = ccxt.binanceusdm({**creds})

        bot_cfg = self.settings.get("bots", {}).get("funding_rate", {})
        position_size = float(bot_cfg.get("position_size_usdt", capital))
        self._funding_interval = int(bot_cfg.get("scan_interval_seconds", 3600))
        self._poll_seconds = int(bot_cfg.get("poll_seconds", 30))
        self._last_funding_scan: float = 0.0

        self.scanner = FundingRateScanner(
            futures_ex=futures_ex,
            spot_ex=spot_ex,
            dry_run=self.dry_run,
            notifier=self.notifier,
            position_size_usdt=position_size,
        )
        self.grid = CexGridBot(
            exchange=spot_ex,
            dry_run=self.dry_run,
            capital_usdt=capital,
            notifier=self.notifier,
        )

        logger.info(
            "FundingRateBot pronto. dry_run=%s capital=%.0fUSDT position_size=%.0fUSDT",
            self.dry_run, capital, position_size,
        )

    def tick(self) -> dict:
        results: dict = {"grid": [], "funding": []}

        results["grid"] = self.grid.tick()

        now = time.time()
        if now - self._last_funding_scan >= self._funding_interval:
            results["funding"] = self.scanner.scan_and_act()
            self._last_funding_scan = now

        return results

    def run_forever(self) -> None:
        logger.info("FundingRateBot a correr em loop (poll=%ss, dry_run=%s).",
                    self._poll_seconds, self.dry_run)
        while True:
            try:
                self.tick()
            except Exception:
                logger.exception("Erro no tick do FundingRateBot")
            time.sleep(self._poll_seconds)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if "--smoke" in sys.argv:
        # Validação rápida: init + um tick + scan forçado
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
        bot = FundingRateBot()
        print(f"dry_run={bot.dry_run}  poll={bot._poll_seconds}s  funding_interval={bot._funding_interval}s")

        # Grid: primeiro tick inicializa a grelha
        grid_r = bot.grid.tick()
        print(f"grid tick (init): {len(grid_r)} sinais, níveis=[{float(bot.grid.grid_prices[0]):.4f}"
              f"..{float(bot.grid.grid_prices[-1]):.4f}]")

        # Funding: forçar scan imediato
        bot._last_funding_scan = 0.0
        r = bot.tick()
        print(f"funding opps acima threshold ({FUNDING_THRESHOLD*100:.2f}%): {len(r['funding'])}")
        print("SMOKE OK")
    else:
        # Modo serviço (chamado pelo systemd ou directamente)
        bot = FundingRateBot()
        bot.run_forever()
