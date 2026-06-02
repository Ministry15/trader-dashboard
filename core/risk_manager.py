"""Gestão de risco: aplica os limites definidos em ``settings.yaml > trading``.

Centraliza todas as decisões de "podemos fazer este trade?": tamanho máximo,
lucro mínimo (em USD e em bps), perda diária máxima, número de posições
abertas e paragem após falhas consecutivas. Mantém estado em memória (perda
diária, posições, falhas) — a persistência fica a cargo de camadas superiores.

Princípio fail-safe: na dúvida, **recusa** o trade.
"""
from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass, field
from decimal import Decimal

from utils.config import get_settings

logger = logging.getLogger(__name__)


@dataclass
class Decision:
    """Resultado de uma verificação de risco."""
    allowed: bool
    reason: str

    def __bool__(self) -> bool:  # permite `if decision:`
        return self.allowed


@dataclass
class RiskManager:
    settings: dict = field(default_factory=get_settings)

    def __post_init__(self):
        t = self.settings["trading"]
        self.dry_run: bool = bool(t.get("dry_run", True))
        self.slippage_bps: int = int(t.get("slippage_tolerance_bps", 50))

        arb = t.get("arbitrage", {})
        self.min_profit_usd = Decimal(str(arb.get("min_profit_usd", 0)))
        self.min_profit_bps = Decimal(str(arb.get("min_profit_bps", 0)))
        self.max_trade_size_usd = Decimal(str(arb.get("max_trade_size_usd", 0)))

        risk = t.get("risk", {})
        self.max_daily_loss_usd = Decimal(str(risk.get("max_daily_loss_usd", 0)))
        self.max_open_positions = int(risk.get("max_open_positions", 1))
        self.stop_on_consecutive_failures = int(risk.get("stop_on_consecutive_failures", 0))

        # estado em memória
        self._day = datetime.date.today()
        self.daily_pnl = Decimal(0)
        self.open_positions = 0
        self.consecutive_failures = 0
        self.halted = False

    # ----------------------------------------------------------------- estado
    def _roll_day(self) -> None:
        """Reinicia os contadores diários quando muda o dia."""
        today = datetime.date.today()
        if today != self._day:
            logger.info("Novo dia (%s): reset do PnL diário (anterior=%s)",
                        today, self.daily_pnl)
            self._day = today
            self.daily_pnl = Decimal(0)

    def can_trade(self) -> Decision:
        """Verificações globais, independentes do trade concreto."""
        self._roll_day()
        if self.halted:
            return Decision(False, "sistema em paragem (halted)")
        if self.max_daily_loss_usd > 0 and self.daily_pnl <= -self.max_daily_loss_usd:
            return Decision(False,
                            f"perda diária atingida ({self.daily_pnl} <= -{self.max_daily_loss_usd} USD)")
        if (self.stop_on_consecutive_failures > 0
                and self.consecutive_failures >= self.stop_on_consecutive_failures):
            return Decision(False,
                            f"{self.consecutive_failures} falhas consecutivas "
                            f"(limite {self.stop_on_consecutive_failures})")
        if self.open_positions >= self.max_open_positions:
            return Decision(False,
                            f"posições abertas no limite ({self.open_positions}/{self.max_open_positions})")
        return Decision(True, "ok")

    # ----------------------------------------------------------- validação trade
    def check_trade(self, trade_size_usd, expected_profit_usd,
                    profit_bps=None) -> Decision:
        """Valida um trade concreto contra todos os limites."""
        gate = self.can_trade()
        if not gate.allowed:
            return gate

        size = Decimal(str(trade_size_usd))
        profit = Decimal(str(expected_profit_usd))

        if size <= 0:
            return Decision(False, "tamanho de trade inválido (<= 0)")
        if self.max_trade_size_usd > 0 and size > self.max_trade_size_usd:
            return Decision(False,
                            f"tamanho {size} > máximo {self.max_trade_size_usd} USD")
        if profit < self.min_profit_usd:
            return Decision(False,
                            f"lucro {profit} < mínimo {self.min_profit_usd} USD")
        if profit_bps is not None and Decimal(str(profit_bps)) < self.min_profit_bps:
            return Decision(False,
                            f"lucro {profit_bps} bps < mínimo {self.min_profit_bps} bps")
        return Decision(True, "ok")

    def check_size(self, trade_size_usd) -> Decision:
        """Gate leve para estratégias não-arbitragem (grid/DCA/sniper).

        Aplica as verificações globais (:meth:`can_trade`) e o limite de
        tamanho, mas **não** exige lucro mínimo.
        """
        gate = self.can_trade()
        if not gate.allowed:
            return gate
        size = Decimal(str(trade_size_usd))
        if size <= 0:
            return Decision(False, "tamanho de trade inválido (<= 0)")
        if self.max_trade_size_usd > 0 and size > self.max_trade_size_usd:
            return Decision(False,
                            f"tamanho {size} > máximo {self.max_trade_size_usd} USD")
        return Decision(True, "ok")

    # ----------------------------------------------------------------- slippage
    def min_amount_out(self, amount_out_wei: int, slippage_bps: int | None = None) -> int:
        """Mínimo aceitável após slippage, em wei (para amountOutMin)."""
        bps = self.slippage_bps if slippage_bps is None else int(slippage_bps)
        return (int(amount_out_wei) * (10_000 - bps)) // 10_000

    # ------------------------------------------------------- registo de execução
    def register_open(self) -> None:
        self.open_positions += 1

    def register_close(self, pnl_usd) -> None:
        """Fecha uma posição e actualiza PnL diário + contador de falhas."""
        self._roll_day()
        pnl = Decimal(str(pnl_usd))
        self.open_positions = max(0, self.open_positions - 1)
        self.daily_pnl += pnl
        if pnl < 0:
            self.consecutive_failures += 1
        else:
            self.consecutive_failures = 0

        if (self.max_daily_loss_usd > 0
                and self.daily_pnl <= -self.max_daily_loss_usd):
            self.halted = True
            logger.warning("HALT: perda diária %s <= -%s USD",
                           self.daily_pnl, self.max_daily_loss_usd)
        if (self.stop_on_consecutive_failures > 0
                and self.consecutive_failures >= self.stop_on_consecutive_failures):
            self.halted = True
            logger.warning("HALT: %s falhas consecutivas", self.consecutive_failures)

    def reset_halt(self) -> None:
        """Levanta a paragem manualmente (intervenção do operador)."""
        self.halted = False
        self.consecutive_failures = 0
        logger.info("Paragem levantada manualmente.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    rm = RiskManager()
    print(f"Limites: max_size={rm.max_trade_size_usd} USD | min_profit={rm.min_profit_usd} USD / "
          f"{rm.min_profit_bps} bps | max_daily_loss={rm.max_daily_loss_usd} USD | "
          f"max_pos={rm.max_open_positions} | stop_fails={rm.stop_on_consecutive_failures}")

    cases = [
        ("trade bom",        dict(trade_size_usd=300, expected_profit_usd=5,  profit_bps=40)),
        ("tamanho excede",   dict(trade_size_usd=999, expected_profit_usd=5,  profit_bps=40)),
        ("lucro USD baixo",  dict(trade_size_usd=300, expected_profit_usd=1,  profit_bps=40)),
        ("lucro bps baixo",  dict(trade_size_usd=300, expected_profit_usd=5,  profit_bps=10)),
    ]
    for name, kw in cases:
        d = rm.check_trade(**kw)
        print(f"  {name:16s} -> allowed={d.allowed}  ({d.reason})")

    # slippage
    print("  min_amount_out(1_000_000 wei):", rm.min_amount_out(1_000_000))

    # simular falhas consecutivas até HALT
    print("  simular falhas consecutivas...")
    for i in range(rm.stop_on_consecutive_failures):
        rm.register_open()
        rm.register_close(-1)
    after = rm.can_trade()
    print(f"  após {rm.stop_on_consecutive_failures} perdas -> can_trade={after.allowed} ({after.reason})")
    assert not after.allowed, "esperava-se HALT após falhas consecutivas"
    print("SMOKE OK")
