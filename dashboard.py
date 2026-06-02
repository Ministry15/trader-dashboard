#!/usr/bin/env python3
"""Dashboard de estado dos bots — visualização por tabs no terminal.

Uso:
    ./venv/bin/python dashboard.py            # imprime estado e sai
    ./venv/bin/python dashboard.py --watch    # actualiza a cada 10s (Ctrl+C para sair)
    ./venv/bin/python dashboard.py grid       # mostra apenas o tab Grid
"""
from __future__ import annotations

import os
import sys
import time
from decimal import Decimal
from typing import Optional

import requests

from utils.config import get_env, get_settings

_W = 72   # largura da caixa


def _line(char: str = "─", width: int = _W) -> str:
    return char * width


def _box_top(title: str = "", width: int = _W) -> str:
    if title:
        pad = width - 4 - len(title)
        return "┌── " + title + " " + "─" * max(0, pad) + "┐"
    return "┌" + "─" * (width - 2) + "┐"


def _box_bot(width: int = _W) -> str:
    return "└" + "─" * (width - 2) + "┘"


def _row(text: str, width: int = _W) -> str:
    inner = width - 4
    return "│  " + text.ljust(inner)[:inner] + "  │"


def _header(width: int = _W) -> str:
    title = " crypto_bsc Dashboard "
    pad = (width - len(title)) // 2
    return "═" * pad + title + "═" * (width - pad - len(title))


def _dry_run_flag() -> bool:
    return str(get_env("DRY_RUN", "true")).lower() not in ("false", "0", "no")


def _fetch_sol_price() -> Optional[Decimal]:
    try:
        resp = requests.get("https://price.jup.ag/v4/price?ids=SOL", timeout=8)
        resp.raise_for_status()
        return Decimal(str(resp.json()["data"]["SOL"]["price"]))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------

def _tab_grid(settings: dict, dry_run: bool) -> list[str]:
    """Tab Grid — 3 subseparadores: BSC WBNB, PEPE, Solana SOL."""
    lines: list[str] = []
    lines.append("")
    lines.append("  ▶  GRID BOTS")
    lines.append("")

    # ── Sub-tab 1: BSC WBNB/USDT ──────────────────────────────────────────
    g = settings["bots"].get("grid", {})
    lines.append(_box_top("BSC  WBNB/USDT  (PancakeSwap V2)"))
    lines.append(_row(f"DEX:       {g.get('dex', 'pancakeswap_v2')}"))
    if "range_pct" in g:
        rng = f"±{float(g['range_pct']) * 100:.0f}% do preço live"
    else:
        rng = f"{g.get('lower_price', '?')} .. {g.get('upper_price', '?')} USDT"
    lines.append(_row(f"Range:     {rng}"))
    lines.append(_row(f"Níveis:    {g.get('grid_levels', '?')}"))
    lines.append(_row(f"Ordem:     ${g.get('order_size_quote', '?')} USDT"))
    lines.append(_row(f"Poll:      {g.get('poll_seconds', 10)}s"))
    lines.append(_row(f"DRY_RUN:   {dry_run}"))
    lines.append(_box_bot())
    lines.append("")

    # ── Sub-tab 2: PEPE/USDT ──────────────────────────────────────────────
    pg = settings["bots"].get("pepe_grid", {})
    lines.append(_box_top("BSC  PEPE/USDT  (PancakeSwap V2)"))
    lines.append(_row(f"DEX:       {pg.get('dex', 'pancakeswap_v2')}"))
    lines.append(_row(f"Range:     ±{float(pg.get('range_pct', 0.20)) * 100:.0f}% do preço live"))
    lines.append(_row(f"Níveis:    {pg.get('grid_levels', '?')}"))
    lines.append(_row(f"Ordem:     ${pg.get('order_size_quote', '?')} USDT"))
    lines.append(_row(f"Poll:      {pg.get('poll_seconds', 10)}s"))
    lines.append(_row(f"DRY_RUN:   {dry_run}"))
    lines.append(_box_bot())
    lines.append("")

    # ── Sub-tab 3: Solana SOL/USDC (Jupiter) ──────────────────────────────
    sg = settings["bots"].get("solana_grid", {})
    sol_price = _fetch_sol_price()
    price_str = f"${float(sol_price):.4f}" if sol_price else "indisponível"

    if sol_price:
        rng_pct = Decimal(str(sg.get("range_pct", "0.15")))
        lower = sol_price * (1 - rng_pct)
        upper = sol_price * (1 + rng_pct)
        range_str = f"${float(lower):.4f} .. ${float(upper):.4f}  (±{float(rng_pct)*100:.0f}%)"
    else:
        range_str = f"±{float(sg.get('range_pct', 0.15)) * 100:.0f}% do preço live"

    wallet = sg.get("wallet", "")
    wallet_short = (wallet[:12] + "…" + wallet[-4:]) if len(wallet) > 20 else wallet

    lines.append(_box_top("Solana  SOL/USDC  (Jupiter)"))
    lines.append(_row(f"Preço live: {price_str}"))
    lines.append(_row(f"Range:      {range_str}"))
    lines.append(_row(f"Níveis:     {sg.get('grid_levels', 20)}"))
    lines.append(_row(f"Ordem:      ${sg.get('order_size_quote', 5)} USDC"))
    lines.append(_row(f"Fee/tx:     $0.0001  (Solana)"))
    lines.append(_row(f"Poll:       {sg.get('poll_seconds', 10)}s"))
    lines.append(_row(f"Wallet:     {wallet_short}"))
    lines.append(_row(f"DRY_RUN:    {dry_run}"))
    lines.append(_box_bot())

    return lines


def _tab_arbitrage(settings: dict, dry_run: bool) -> list[str]:
    arb = settings.get("trading", {}).get("arbitrage", {})
    lines = ["", "  ▶  ARBITRAGE", ""]
    lines.append(_box_top("BSC Multi-DEX Arbitrage"))
    lines.append(_row(f"Min profit:  ${arb.get('min_profit_usd', '?')} / {arb.get('min_profit_bps', '?')}bps"))
    lines.append(_row(f"Max trade:   ${arb.get('max_trade_size_usd', '?')}"))
    lines.append(_row(f"DRY_RUN:     {dry_run}"))
    lines.append(_box_bot())
    return lines


def _tab_dca(settings: dict, dry_run: bool) -> list[str]:
    dca = settings["bots"].get("dca", {})
    lines = ["", "  ▶  DCA", ""]
    lines.append(_box_top(f"BSC  {dca.get('base','?')}/{dca.get('quote','?')}"))
    lines.append(_row(f"Valor/compra:  ${dca.get('amount_quote', '?')} {dca.get('quote','?')}"))
    lines.append(_row(f"Intervalo:     {dca.get('interval_seconds', 86400)}s"))
    lines.append(_row(f"DEX:           {dca.get('dex', '?')}"))
    lines.append(_row(f"DRY_RUN:       {dry_run}"))
    lines.append(_box_bot())
    return lines


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

TAB_LABELS = ["Arbitrage", "Grid", "DCA"]


def _render(tab: Optional[str], settings: dict, dry_run: bool) -> list[str]:
    out: list[str] = []

    # cabeçalho
    from datetime import datetime
    out.append("")
    out.append(_header())
    out.append(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  |  DRY_RUN={dry_run}")
    out.append(_line())

    # tab bar
    tabs_str = "   ".join(
        f"[{t}]" if (tab is None and i == 1) or (tab and tab.lower() == t.lower())
        else f" {t} "
        for i, t in enumerate(TAB_LABELS)
    )
    out.append(f"  {tabs_str}")
    out.append(_line())

    want = tab.lower() if tab else "grid"

    if want in ("grid", "all"):
        out.extend(_tab_grid(settings, dry_run))
    if want in ("arbitrage", "all"):
        out.extend(_tab_arbitrage(settings, dry_run))
    if want in ("dca", "all"):
        out.extend(_tab_dca(settings, dry_run))

    out.append("")
    return out


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    watch = "--watch" in argv
    tab = next((a for a in argv if not a.startswith("-")), None)

    settings = get_settings()
    dry_run = _dry_run_flag()

    try:
        while True:
            if watch:
                os.system("clear" if os.name != "nt" else "cls")
            for line in _render(tab, settings, dry_run):
                print(line)
            if not watch:
                break
            time.sleep(10)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
