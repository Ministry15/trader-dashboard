"""Grid bot: compra em quedas e vende em subidas, numa grelha de preços.

Como os DEXs não têm ordens-limite, o bot faz polling do preço e executa swaps
de mercado quando o preço **cruza** uma linha da grelha: cada cruzamento para
baixo dispara uma compra de ``order_size_quote``; cada cruzamento para cima
dispara uma venda do equivalente. Toda a execução respeita o dry-run.

Recentering dinâmico:
    Se o preço sair do range em mais de ``recenter_threshold_pct``% (padrão 5%),
    o grid é recentrado automaticamente no novo preço.  Para bots com
    ``range_pct`` definido, o novo range é ±range_pct%; para bots com
    ``lower_price``/``upper_price`` fixos, mantém a mesma largura mas desloca.
    O Telegram é notificado sempre que ocorre um recentering.
    O histórico de trades fica intacto na base de dados.
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
    def __init__(self, settings: dict | None = None, config_key: str = "grid"):
        self.settings = settings or get_settings()
        cfg = self.settings["bots"][config_key]
        self.bot_name = config_key
        self.base = cfg["base"]
        self.quote = cfg["quote"]
        self.order_size_quote = Decimal(str(cfg["order_size_quote"]))
        self.poll_seconds = int(cfg.get("poll_seconds", 10))
        self._recenter_threshold = (
            Decimal(str(cfg.get("recenter_threshold_pct", 5))) / 100
        )

        self.wallet = Wallet(self.settings)
        self.dex = Dex(cfg["dex"], wallet=self.wallet, settings=self.settings)
        self.risk = RiskManager(settings=self.settings)
        self.notifier = TelegramNotifier(self.settings)
        self.router = self.dex.cfg["router"]
        self.prev_price: Decimal | None = None
        database.init_db()

        n_levels = int(cfg["grid_levels"])

        if "range_pct" in cfg:
            # Range dinâmico: calculado a partir do preço live no arranque
            self._range_pct: Decimal | None = Decimal(str(cfg["range_pct"]))
            price = self.dex.get_price(self.base, self.quote)
            lower = price * (1 - self._range_pct)
            upper = price * (1 + self._range_pct)
            logger.info(
                "Range dinâmico %s/%s: preço=%.8f range_pct=%.0f%% -> [%.8f..%.8f]",
                self.base, self.quote, price, float(self._range_pct) * 100, lower, upper,
            )
        else:
            self._range_pct = None
            lower = Decimal(str(cfg["lower_price"]))
            upper = Decimal(str(cfg["upper_price"]))

        self.levels = build_levels(lower, upper, n_levels)

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

    # --------------------------------------------------------- recentering
    def _maybe_recenter(self, price: Decimal) -> bool:
        """Recentra o grid se o preço saiu do range em mais de threshold %.

        Devolve True se o recentering ocorreu (e tick() deve saltar cruzamentos).
        """
        lower = self.levels[0]
        upper = self.levels[-1]
        below = price < lower * (1 - self._recenter_threshold)
        above = price > upper * (1 + self._recenter_threshold)
        if not (below or above):
            return False

        n = len(self.levels)
        if self._range_pct is not None:
            # Bots dinâmicos: reconstrói ±range_pct ao redor do novo preço
            new_lower = price * (1 - self._range_pct)
            new_upper = price * (1 + self._range_pct)
        else:
            # Bots estáticos: mantém largura, desloca centro
            half = (upper - lower) / 2
            new_lower = price - half
            new_upper = price + half

        self.levels = build_levels(new_lower, new_upper, n)
        self.prev_price = None  # evita cruzamentos espúrios no próximo tick

        direction = "abaixo" if below else "acima"
        msg = (
            f"Grid {self.base}/{self.quote} recentrada — "
            f"preço {float(price):.8f} saiu {direction} do range "
            f"[{float(lower):.8f}..{float(upper):.8f}]. "
            f"Novo range: [{float(new_lower):.8f}..{float(new_upper):.8f}]"
        )
        logger.info(msg)
        self.notifier.notify("grid_recentered", msg)
        return True

    # ------------------------------------------------------------- execução
    def _execute(self, side: str, level: Decimal, price: Decimal) -> dict:
        if side == "buy":
            size_usd = self.order_size_quote
            decision = self.risk.check_size(size_usd)
            if not decision.allowed:
                logger.info("Compra recusada @ %.8f: %s", level, decision.reason)
                return {"executed": False, "reason": decision.reason}
            self.wallet.ensure_allowance(self.quote, self.router, self.order_size_quote)
            tx = self.dex.build_swap_tx(self.quote, self.base, self.order_size_quote)
        else:  # sell
            base_amount = self.order_size_quote / price
            decision = self.risk.check_size(self.order_size_quote)
            if not decision.allowed:
                logger.info("Venda recusada @ %.8f: %s", level, decision.reason)
                return {"executed": False, "reason": decision.reason}
            self.wallet.ensure_allowance(self.base, self.router, base_amount)
            tx = self.dex.build_swap_tx(self.base, self.quote, base_amount)

        result = self.wallet.send_transaction(tx)
        database.record_trade(
            bot=self.bot_name, base=self.base, quote=self.quote,
            dex_buy=self.dex.name if side == "buy" else None,
            dex_sell=self.dex.name if side == "sell" else None,
            size_usd=float(self.order_size_quote),
            dry_run=self.wallet.dry_run,
            status="dry_run" if self.wallet.dry_run else "sent",
        )
        self.notifier.notify(
            "trade_executed",
            f"GRID {side.upper()} {self.base}/{self.quote} @ ~{float(price):.8f} "
            f"(nível {float(level):.8f}, dry_run={self.wallet.dry_run})",
        )
        logger.info(
            "GRID %s @ nível %.8f (preço ~%.8f) dry_run=%s",
            side.upper(), level, price, self.wallet.dry_run,
        )
        return {"executed": True, "side": side, "level": level, "result": result}

    def tick(self) -> list[dict]:
        price = self.dex.get_price(self.base, self.quote)

        if self._maybe_recenter(price):
            return []  # grid recentrado; salta cruzamentos neste tick

        if self.prev_price is None:
            self.prev_price = price
            logger.info(
                "Grid %s/%s iniciada. Preço de referência: %.8f range [%.8f..%.8f]",
                self.base, self.quote, price, self.levels[0], self.levels[-1],
            )
            return []

        signals = self.crossings(self.prev_price, price)
        self.prev_price = price
        return [self._execute(side, level, price) for side, level in signals]

    def run_forever(self) -> None:
        logger.info(
            "GridBot (%s) a correr: %s/%s %s níveis [%.8f..%.8f], dry_run=%s",
            self.bot_name, self.base, self.quote,
            len(self.levels), self.levels[0], self.levels[-1], self.wallet.dry_run,
        )
        while True:
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                logger.exception("Erro no tick da grelha")
            time.sleep(self.poll_seconds)


if __name__ == "__main__":
    bot = GridBot()
    print("níveis:", [f"{l:.0f}" for l in bot.levels])
    print("queda 760->690:", bot.crossings(Decimal("760"), Decimal("690")))
    print("subida 690->760:", bot.crossings(Decimal("690"), Decimal("760")))
    print("sem cruzar 705->706:", bot.crossings(Decimal("705"), Decimal("706")))
    live = bot.dex.get_price(bot.base, bot.quote)
    print(f"preço live {bot.base}/{bot.quote}: {live:.2f}")
    r = bot._execute("buy", bot.levels[0], live)
    print("execução compra (dry-run):", {k: r[k] for k in r if k != "result"})
    print("SMOKE OK")
