"""Grid bot especializado para PEPE/USDT.

Range dinâmico ±20% do preço live no arranque, com recentering automático
se o preço sair do range em mais de 5%.  Parâmetros em settings.yaml > bots.pepe_grid.

Herda toda a lógica de GridBot (crossings, recentering, execução) —
a única diferença é a chave de configuração ("pepe_grid" em vez de "grid").
"""
from __future__ import annotations

from bots.grid_bot import GridBot
from utils.config import get_settings


class PepeGridBot(GridBot):
    """Grid bot para PEPE/USDT com range dinâmico ±20% e recentering automático."""

    def __init__(self, settings: dict | None = None):
        super().__init__(settings=settings or get_settings(), config_key="pepe_grid")


if __name__ == "__main__":
    from decimal import Decimal

    bot = PepeGridBot()
    print(f"Par: {bot.base}/{bot.quote}")
    print(f"Níveis: {len(bot.levels)}")
    print(f"Range: [{float(bot.levels[0]):.10f}..{float(bot.levels[-1]):.10f}]")
    print(f"Ordem: ${float(bot.order_size_quote):.2f} USDT")
    print(f"dry_run: {bot.wallet.dry_run}")
    print("SMOKE OK")
