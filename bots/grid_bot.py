"""Grid bot: compra em quedas e vende em subidas, numa grelha de preços fixa.

Como os DEXs não têm ordens-limite, o bot faz polling do preço e executa swaps
de mercado quando o preço **cruza** uma linha da grelha: cada cruzamento para
baixo dispara uma compra de ``order_size_quote``; cada cruzamento para cima
dispara uma venda do equivalente. Toda a execução respeita o dry-run.
"""
from __future__ import annotations

import time
from decimal import Decimal

from core.dex import Dex
from core.risk_manager import RiskManager
from core.wallet import Wallet
from utils import database
from utils.config import get_settings
from utils.logger import get_logger
from utils.notifier import TelegramNotifier

logger = get_logger(__name__)


def build_levels(lower: Decimal, upper: Decimal, n: int) -> list[Decimal]:
    """``n`` linhas igualmente espaçadas entre ``lower`` e ``upper`` (inclusive)."""
    if n < 2 or upper <= lower:
        raise ValueError("grid inválida: requer n>=2 e upper>lower")
    step = (upper - lower) / (n - 1)
    return [lower + step * i for i in range(n)]


class GridBot:
    def __init__(self, settings: dict | None = None):
        self.settings = settings or get_settings()
        cfg = self.settings["bots"]["grid"]
        self.base = cfg["base"]
        self.quote = cfg["quote"]
        self.order_size_quote = Decimal(str(cfg["order_size_quote"]))
        self.poll_seconds = int(cfg.get("poll_seconds", 10))
        self.levels = build_levels(Decimal(str(cfg["lower_price"])),
                                   Decimal(str(cfg["upper_price"])),
                                   int(cfg["grid_levels"]))

        self.wallet = Wallet(self.settings)
        self.dex = Dex(cfg["dex"], wallet=self.wallet, settings=self.settings)
        self.risk = RiskManager(settings=self.settings)
        self.notifier = TelegramNotifier(self.settings)
        self.router = self.dex.cfg["router"]
        self.prev_price: Decimal | None = None
        database.init_db()

    # --------------------------------------------------------------- sinais
    def crossings(self, prev: Decimal, cur: Decimal) -> list[tuple[str, Decimal]]:
        """Linhas cruzadas entre ``prev`` e ``cur`` -> [('buy'|'sell', nível)]."""
        out = []
        for g in self.levels:
            if prev > g >= cur:        # desceu através de g
                out.append(("buy", g))
            elif prev < g <= cur:      # subiu através de g
                out.append(("sell", g))
        return out

    # ------------------------------------------------------------- execução
    def _execute(self, side: str, level: Decimal, price: Decimal) -> dict:
        if side == "buy":
            size_usd = self.order_size_quote
            decision = self.risk.check_size(size_usd)
            if not decision.allowed:
                logger.info("Compra recusada @ %.2f: %s", level, decision.reason)
                return {"executed": False, "reason": decision.reason}
            self.wallet.ensure_allowance(self.quote, self.router, self.order_size_quote)
            tx = self.dex.build_swap_tx(self.quote, self.base, self.order_size_quote)
        else:  # sell
            base_amount = self.order_size_quote / price
            decision = self.risk.check_size(self.order_size_quote)
            if not decision.allowed:
                logger.info("Venda recusada @ %.2f: %s", level, decision.reason)
                return {"executed": False, "reason": decision.reason}
            self.wallet.ensure_allowance(self.base, self.router, base_amount)
            tx = self.dex.build_swap_tx(self.base, self.quote, base_amount)

        result = self.wallet.send_transaction(tx)
        database.record_trade(bot="grid", base=self.base, quote=self.quote,
                              dex_buy=self.dex.name if side == "buy" else None,
                              dex_sell=self.dex.name if side == "sell" else None,
                              size_usd=float(self.order_size_quote), dry_run=self.wallet.dry_run,
                              status="dry_run" if self.wallet.dry_run else "sent")
        self.notifier.notify("trade_executed",
                             f"GRID {side.upper()} {self.base}/{self.quote} @ ~{price:.2f} "
                             f"(nível {level:.2f}, dry_run={self.wallet.dry_run})")
        logger.info("GRID %s @ nível %.2f (preço ~%.2f) dry_run=%s",
                    side.upper(), level, price, self.wallet.dry_run)
        return {"executed": True, "side": side, "level": level, "result": result}

    def tick(self) -> list[dict]:
        price = self.dex.get_price(self.base, self.quote)
        if self.prev_price is None:
            self.prev_price = price
            logger.info("Grid iniciada. Preço de referência: %.2f", price)
            return []
        signals = self.crossings(self.prev_price, price)
        self.prev_price = price
        return [self._execute(side, level, price) for side, level in signals]

    def run_forever(self) -> None:
        logger.info("GridBot a correr: %s níveis [%.2f..%.2f], dry_run=%s",
                    len(self.levels), self.levels[0], self.levels[-1], self.wallet.dry_run)
        while True:
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                logger.exception("Erro no tick da grelha")
            time.sleep(self.poll_seconds)


if __name__ == "__main__":
    bot = GridBot()
    print("níveis:", [f"{l:.0f}" for l in bot.levels])
    # 1) lógica de cruzamentos (sintético, determinístico)
    print("queda 760->690:", bot.crossings(Decimal("760"), Decimal("690")))
    print("subida 690->760:", bot.crossings(Decimal("690"), Decimal("760")))
    print("sem cruzar 705->706:", bot.crossings(Decimal("705"), Decimal("706")))
    # 2) caminho de execução em dry-run (compra forçada)
    live = bot.dex.get_price(bot.base, bot.quote)
    print(f"preço live {bot.base}/{bot.quote}: {live:.2f}")
    r = bot._execute("buy", bot.levels[0], live)
    print("execução compra (dry-run):", {k: r[k] for k in r if k != "result"})
    print("SMOKE OK")
