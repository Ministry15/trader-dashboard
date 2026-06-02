"""Sniper bot: entra em tokens-alvo e gere a saída por take-profit / stop-loss.

Para cada token vigiado (``target_tokens``), o bot verifica se existe pool com
``quote`` (normalmente WBNB) e se o **price impact** de comprar
``buy_amount_quote`` está abaixo do limite; se sim, executa a entrada. Depois,
em cada tick, reavalia o valor da posição e vende ao atingir o take-profit ou o
stop-loss. Tudo dry-run-safe.

Aviso: este sniper opera sobre tokens já listados/com pool. Não faz detecção de
novos pares via mempool (isso exigiria um nó/stream de eventos dedicado).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal

from core.dex import Dex
from core.risk_manager import RiskManager
from core.wallet import Wallet
from utils import database
from utils.config import get_settings
from utils.logger import get_logger
from utils.notifier import TelegramNotifier

logger = get_logger(__name__)


@dataclass
class Position:
    token: str
    token_amount: Decimal      # quantidade de token detida
    spent_quote: Decimal       # quote gasto na entrada
    entry_value: Decimal = field(default=Decimal(0))


class SniperBot:
    def __init__(self, settings: dict | None = None, targets: list[str] | None = None):
        self.settings = settings or get_settings()
        cfg = self.settings["bots"]["sniper"]
        self.quote = cfg["quote"]
        self.buy_amount = Decimal(str(cfg["buy_amount_quote"]))
        self.max_impact_bps = Decimal(str(cfg["max_price_impact_bps"]))
        self.tp_bps = Decimal(str(cfg["take_profit_bps"]))
        self.sl_bps = Decimal(str(cfg["stop_loss_bps"]))
        self.poll_seconds = int(cfg.get("poll_seconds", 5))
        self.targets = list(targets if targets is not None else cfg.get("target_tokens", []))

        self.wallet = Wallet(self.settings)
        self.dex = Dex(cfg["dex"], wallet=self.wallet, settings=self.settings)
        self.risk = RiskManager(settings=self.settings)
        self.notifier = TelegramNotifier(self.settings)
        self.router = self.dex.cfg["router"]
        self.positions: dict[str, Position] = {}
        database.init_db()

    # --------------------------------------------------------------- utils
    def _size_usd(self) -> Decimal:
        """Converte o buy_amount (em quote) para USD, p/ o gate de risco."""
        if self.quote in ("USDT", "USDC", "BUSD"):
            return self.buy_amount
        wbnb_usd = self.dex.get_price("WBNB", "USDT")
        return self.buy_amount * wbnb_usd  # quote == WBNB no caso típico

    def _decide_exit(self, pnl_bps: Decimal) -> str | None:
        if pnl_bps >= self.tp_bps:
            return "take_profit"
        if pnl_bps <= -self.sl_bps:
            return "stop_loss"
        return None

    # -------------------------------------------------------------- entrada
    def consider(self, token: str) -> dict:
        """Avalia e, se passar, executa a entrada num token."""
        if token in self.positions:
            return {"action": "skip", "reason": "já em posição"}
        if not self.dex.has_liquidity(self.quote, token):
            return {"action": "skip", "reason": "sem pool"}
        impact = self.dex.price_impact_bps(self.quote, token, self.buy_amount)
        if impact > self.max_impact_bps:
            return {"action": "skip", "reason": f"impacto {impact:.0f}bps > {self.max_impact_bps:.0f}bps"}

        decision = self.risk.check_size(self._size_usd())
        if not decision.allowed:
            return {"action": "skip", "reason": decision.reason}

        token_amount = self.dex.quote(self.quote, token, self.buy_amount)
        self.wallet.ensure_allowance(self.quote, self.router, self.buy_amount)
        tx = self.dex.build_swap_tx(self.quote, token, self.buy_amount)
        result = self.wallet.send_transaction(tx)

        pos = Position(token=token, token_amount=token_amount,
                       spent_quote=self.buy_amount, entry_value=self.buy_amount)
        self.positions[token] = pos
        self.risk.register_open()
        database.record_trade(bot="sniper", base=token, quote=self.quote,
                              dex_buy=self.dex.name, size_usd=float(self._size_usd()),
                              dry_run=self.wallet.dry_run,
                              status="dry_run" if self.wallet.dry_run else "sent")
        self.notifier.notify("trade_executed",
                             f"SNIPE BUY {token} com {self.buy_amount} {self.quote} "
                             f"(impacto {impact:.0f}bps, dry_run={self.wallet.dry_run})")
        logger.info("SNIPE BUY %s: %s %s -> ~%.6f token (impacto %.0fbps)",
                    token, self.buy_amount, self.quote, token_amount, impact)
        return {"action": "buy", "impact_bps": impact, "token_amount": token_amount,
                "result": result}

    # --------------------------------------------------------------- gestão
    def manage(self, token: str) -> dict:
        """Reavalia uma posição e sai por TP/SL se aplicável."""
        pos = self.positions[token]
        current_value = self.dex.quote(token, self.quote, pos.token_amount)
        pnl = current_value - pos.spent_quote
        pnl_bps = (pnl / pos.spent_quote) * Decimal(10_000) if pos.spent_quote else Decimal(0)
        action = self._decide_exit(pnl_bps)
        if action is None:
            return {"action": "hold", "pnl_bps": pnl_bps}

        self.wallet.ensure_allowance(token, self.router, pos.token_amount)
        tx = self.dex.build_swap_tx(token, self.quote, pos.token_amount)
        result = self.wallet.send_transaction(tx)
        del self.positions[token]
        self.risk.register_close(float(pnl))   # PnL em quote (~estimado em dry-run)
        database.record_trade(bot="sniper", base=token, quote=self.quote,
                              dex_sell=self.dex.name, size_usd=float(pos.spent_quote),
                              profit_usd=float(pnl), profit_bps=float(pnl_bps),
                              dry_run=self.wallet.dry_run, status=action)
        self.notifier.notify("trade_executed",
                             f"SNIPE {action.upper()} {token}: PnL {pnl:+.6f} {self.quote} "
                             f"({pnl_bps:+.0f}bps, dry_run={self.wallet.dry_run})")
        logger.info("SNIPE %s %s: PnL %+.6f %s (%+.0fbps)",
                    action.upper(), token, pnl, self.quote, pnl_bps)
        return {"action": action, "pnl_bps": pnl_bps, "pnl": pnl, "result": result}

    def tick(self) -> list[dict]:
        out = []
        for token in self.targets:
            if token not in self.positions:
                out.append({token: self.consider(token)})
        for token in list(self.positions):
            out.append({token: self.manage(token)})
        return out

    def run_forever(self) -> None:
        logger.info("SniperBot a correr: %d alvos, impacto<=%.0fbps, TP=%.0fbps SL=%.0fbps, dry_run=%s",
                    len(self.targets), self.max_impact_bps, self.tp_bps, self.sl_bps,
                    self.wallet.dry_run)
        while True:
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                logger.exception("Erro no tick do sniper")
            time.sleep(self.poll_seconds)


if __name__ == "__main__":
    # Sem alvos reais no settings: usamos CAKE (líquido) como alvo de demonstração.
    bot = SniperBot(targets=["CAKE"])
    print(f"quote={bot.quote} buy_amount={bot.buy_amount} max_impact={bot.max_impact_bps}bps "
          f"TP={bot.tp_bps} SL={bot.sl_bps} dry_run={bot.wallet.dry_run}")
    # 1) lógica de saída (sintético)
    print("decide_exit(+6000):", bot._decide_exit(Decimal("6000")))
    print("decide_exit(-2500):", bot._decide_exit(Decimal("-2500")))
    print("decide_exit(+100):", bot._decide_exit(Decimal("100")))
    # 2) entrada live em dry-run sobre CAKE
    r_buy = bot.consider("CAKE")
    print("consider(CAKE):", {k: (round(r_buy[k], 4) if isinstance(r_buy.get(k), Decimal) else r_buy[k])
                              for k in r_buy if k != "result"})
    # 3) gerir a posição acabada de abrir (deve manter — PnL ~0)
    if "CAKE" in bot.positions:
        r_manage = bot.manage("CAKE")
        print("manage(CAKE):", {"action": r_manage["action"],
                                "pnl_bps": round(r_manage["pnl_bps"], 1)})
    print("SMOKE OK")
