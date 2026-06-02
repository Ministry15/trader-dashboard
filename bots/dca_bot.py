"""DCA bot: compra um montante fixo de ``base`` em intervalos regulares.

Dollar-Cost Averaging — ignora o timing e compra ``amount_quote`` de ``base``
a cada ``interval_seconds``, suavizando o preço médio de entrada. Mantém
estatísticas acumuladas (investido, base adquirida, preço médio). Execução
sempre dry-run-safe.
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


class DCABot:
    def __init__(self, settings: dict | None = None):
        self.settings = settings or get_settings()
        cfg = self.settings["bots"]["dca"]
        self.base = cfg["base"]
        self.quote = cfg["quote"]
        self.amount_quote = Decimal(str(cfg["amount_quote"]))
        self.interval_seconds = int(cfg.get("interval_seconds", 86400))

        self.wallet = Wallet(self.settings)
        self.dex = Dex(cfg["dex"], wallet=self.wallet, settings=self.settings)
        self.risk = RiskManager(settings=self.settings)
        self.notifier = TelegramNotifier(self.settings)
        self.router = self.dex.cfg["router"]

        # estatísticas acumuladas
        self.total_quote = Decimal(0)
        self.total_base = Decimal(0)
        self.buys = 0
        database.init_db()

    @property
    def avg_price(self) -> Decimal | None:
        return (self.total_quote / self.total_base) if self.total_base > 0 else None

    def buy_once(self) -> dict:
        """Executa uma compra de DCA (dry-run-safe)."""
        decision = self.risk.check_size(self.amount_quote)
        if not decision.allowed:
            logger.info("Compra DCA recusada: %s", decision.reason)
            return {"executed": False, "reason": decision.reason}

        price = self.dex.get_price(self.base, self.quote)
        # base esperada (em dry-run usamos a cotação; em real viria do recibo)
        expected_base = self.dex.quote(self.quote, self.base, self.amount_quote)

        self.wallet.ensure_allowance(self.quote, self.router, self.amount_quote)
        tx = self.dex.build_swap_tx(self.quote, self.base, self.amount_quote)
        result = self.wallet.send_transaction(tx)

        self.total_quote += self.amount_quote
        self.total_base += expected_base
        self.buys += 1

        database.record_trade(bot="dca", base=self.base, quote=self.quote,
                              dex_buy=self.dex.name, size_usd=float(self.amount_quote),
                              dry_run=self.wallet.dry_run,
                              status="dry_run" if self.wallet.dry_run else "sent")
        self.notifier.notify("trade_executed",
                             f"DCA BUY {self.amount_quote} {self.quote} de {self.base} "
                             f"@ ~{price:.2f} (médio {self.avg_price:.2f}, dry_run={self.wallet.dry_run})")
        logger.info("DCA #%d: +%s %s -> ~%.6f %s @ %.2f | médio=%.2f",
                    self.buys, self.amount_quote, self.quote, expected_base,
                    self.base, price, self.avg_price)
        return {"executed": True, "price": price, "expected_base": expected_base,
                "avg_price": self.avg_price, "result": result}

    def run_forever(self) -> None:
        logger.info("DCABot a correr: %s %s/%s a cada %ss, dry_run=%s",
                    self.amount_quote, self.quote, self.base,
                    self.interval_seconds, self.wallet.dry_run)
        while True:
            try:
                self.buy_once()
            except Exception:  # noqa: BLE001
                logger.exception("Erro na compra DCA")
            time.sleep(self.interval_seconds)


if __name__ == "__main__":
    bot = DCABot()
    print(f"DCA {bot.amount_quote} {bot.quote} -> {bot.base} via {bot.dex.name}, "
          f"intervalo={bot.interval_seconds}s, dry_run={bot.wallet.dry_run}")
    # duas compras simuladas para exercitar a média acumulada
    for _ in range(2):
        r = bot.buy_once()
        print("  buy:", {k: (round(r[k], 4) if isinstance(r.get(k), Decimal) else r[k])
                         for k in ("executed", "price", "expected_base", "avg_price")})
    print(f"acumulado: {bot.buys} compras | investido={bot.total_quote} {bot.quote} | "
          f"base={bot.total_base:.6f} {bot.base} | médio={bot.avg_price:.2f}")
    print("SMOKE OK")
