"""Grid bot para SOL/USDC via Jupiter DEX (Solana).

Estratégia idêntica à do GridBot BSC mas adaptada a Solana:
- Preço obtido do Jupiter Price API (https://price.jup.ag/v4/price?ids=SOL)
- Execução em DRY_RUN: ordens logadas mas NÃO enviadas (sem solders/solana-py)
- Fees Solana: ~$0.0001 por transacção (vs gas BSC)
- Wallet: endereço Solana configurado em settings.yaml > bots.solana_grid

Range dinâmico ±15% do preço live no arranque, com recentering automático
se o preço sair do range em mais de recenter_threshold_pct (padrão 5%).
Compatível com o BOT_REGISTRY de main.py via método tick().
"""
from __future__ import annotations

import time
from decimal import Decimal, InvalidOperation

import requests

from utils.config import get_env, get_settings
from utils.database import init_db, record_trade
from utils.logger import get_logger
from utils.notifier import TelegramNotifier

logger = get_logger(__name__)

JUPITER_PRICE_URL = "https://price.jup.ag/v4/price?ids=SOL"
SOL_FEE_USD = Decimal("0.0001")          # custo fixo por transacção Solana
_PRICE_FALLBACK = Decimal("150")         # usado se a API falhar no arranque


def _dry_run_flag() -> bool:
    return str(get_env("DRY_RUN", "true")).lower() not in ("false", "0", "no")


def _build_levels(lower: Decimal, upper: Decimal, n: int) -> list[Decimal]:
    if n < 2 or upper <= lower:
        raise ValueError(f"Grid inválida: n={n}, lower={lower}, upper={upper}")
    step = (upper - lower) / (n - 1)
    return [lower + step * i for i in range(n)]


class SolanaGridBot:
    """Grid bot SOL/USDC usando Jupiter como fonte de preço."""

    def __init__(self, settings: dict | None = None):
        self.settings = settings or get_settings()
        cfg = self.settings["bots"]["solana_grid"]

        self.base: str = cfg.get("base", "SOL")
        self.quote: str = cfg.get("quote", "USDC")
        self.wallet_address: str = cfg.get("wallet", "")
        self.range_pct = Decimal(str(cfg.get("range_pct", "0.15")))
        self.grid_levels_n = int(cfg.get("grid_levels", 20))
        self.order_size_quote = Decimal(str(cfg.get("order_size_quote", "5")))
        self.poll_seconds = int(cfg.get("poll_seconds", 10))
        self._recenter_threshold = (
            Decimal(str(cfg.get("recenter_threshold_pct", "5"))) / 100
        )

        self.dry_run = _dry_run_flag()
        self.notifier = TelegramNotifier(self.settings)
        init_db()

        price = self._fetch_price()
        lower = price * (1 - self.range_pct)
        upper = price * (1 + self.range_pct)
        self.levels = _build_levels(lower, upper, self.grid_levels_n)
        self.prev_price: Decimal | None = None

        short_wallet = (self.wallet_address[:8] + "…") if self.wallet_address else "(none)"
        logger.info(
            "SolanaGridBot %s/%s: preço=%.4f range=±%.0f%% %d níveis "
            "[%.4f..%.4f] $%.2f/ordem fee=$%.4f dry_run=%s wallet=%s",
            self.base, self.quote, float(price),
            float(self.range_pct) * 100, self.grid_levels_n,
            float(self.levels[0]), float(self.levels[-1]),
            float(self.order_size_quote), float(SOL_FEE_USD),
            self.dry_run, short_wallet,
        )

    # ------------------------------------------------------------------ preço

    def _fetch_price(self) -> Decimal:
        try:
            resp = requests.get(JUPITER_PRICE_URL, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            return Decimal(str(data["data"]["SOL"]["price"]))
        except (KeyError, InvalidOperation, requests.RequestException, ValueError) as exc:
            logger.warning("Jupiter price fetch falhou: %s — fallback %.2f", exc, _PRICE_FALLBACK)
            return _PRICE_FALLBACK

    # ----------------------------------------------------------- cruzamentos

    def crossings(self, prev: Decimal, cur: Decimal) -> list[tuple[str, Decimal]]:
        """Linhas cruzadas entre ``prev`` e ``cur`` -> [('buy'|'sell', nível)]."""
        out = []
        for g in self.levels:
            if prev > g >= cur:        # desceu através de g → comprar
                out.append(("buy", g))
            elif prev < g <= cur:      # subiu através de g → vender
                out.append(("sell", g))
        return out

    # ---------------------------------------------------------- recentering

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

        new_lower = price * (1 - self.range_pct)
        new_upper = price * (1 + self.range_pct)
        self.levels = _build_levels(new_lower, new_upper, self.grid_levels_n)
        self.prev_price = None

        direction = "abaixo" if below else "acima"
        msg = (
            f"SolanaGrid {self.base}/{self.quote} recentrada — "
            f"preço {float(price):.4f} saiu {direction} do range "
            f"[{float(lower):.4f}..{float(upper):.4f}]. "
            f"Novo range: [{float(new_lower):.4f}..{float(new_upper):.4f}]"
        )
        logger.info(msg)
        self.notifier.notify("grid_recentered", msg)
        return True

    # ------------------------------------------------------------ execução

    def _execute(self, side: str, level: Decimal, price: Decimal) -> dict:
        short_wallet = (self.wallet_address[:8] + "…") if self.wallet_address else "(none)"

        if self.dry_run:
            logger.info(
                "[DRY_RUN] Solana GRID %s %s/%s @ ~%.4f "
                "(nível %.4f, size=$%.2f, fee=$%.4f, wallet=%s)",
                side.upper(), self.base, self.quote, float(price),
                float(level), float(self.order_size_quote), float(SOL_FEE_USD),
                short_wallet,
            )
        else:
            # Execução real requer solders/solana-py — fora do âmbito DRY_RUN
            logger.warning(
                "DRY_RUN=false: execução real Solana não implementada "
                "(requer solders/solana-py). Ordem %s @ %.4f ignorada.",
                side, float(level),
            )

        record_trade(
            bot="solana_grid",
            base=self.base,
            quote=self.quote,
            dex_buy="jupiter" if side == "buy" else None,
            dex_sell="jupiter" if side == "sell" else None,
            size_usd=float(self.order_size_quote),
            dry_run=self.dry_run,
            status="dry_run" if self.dry_run else "skipped",
        )
        self.notifier.notify(
            "trade_executed",
            f"Solana GRID {side.upper()} {self.base}/{self.quote} @ ~{float(price):.4f} "
            f"(nível {float(level):.4f}, fee=${float(SOL_FEE_USD):.4f}, dry_run={self.dry_run})",
        )
        return {
            "executed": True,
            "side": side,
            "level": float(level),
            "price": float(price),
            "size_usd": float(self.order_size_quote),
            "fee_usd": float(SOL_FEE_USD),
            "dry_run": self.dry_run,
        }

    # --------------------------------------------------------------- tick

    def tick(self) -> list[dict]:
        price = self._fetch_price()

        if self._maybe_recenter(price):
            return []

        if self.prev_price is None:
            self.prev_price = price
            logger.info(
                "SolanaGrid %s/%s iniciada. Preço: %.4f range [%.4f..%.4f]",
                self.base, self.quote, float(price),
                float(self.levels[0]), float(self.levels[-1]),
            )
            return []

        signals = self.crossings(self.prev_price, price)
        self.prev_price = price
        return [self._execute(side, level, price) for side, level in signals]

    def run_forever(self) -> None:
        logger.info(
            "SolanaGridBot %s/%s a correr: %d níveis [%.4f..%.4f], "
            "$%.2f/ordem, dry_run=%s",
            self.base, self.quote, len(self.levels),
            float(self.levels[0]), float(self.levels[-1]),
            float(self.order_size_quote), self.dry_run,
        )
        while True:
            try:
                self.tick()
            except Exception:
                logger.exception("Erro no tick do SolanaGridBot")
            time.sleep(self.poll_seconds)


if __name__ == "__main__":
    bot = SolanaGridBot()
    print(f"Par:     {bot.base}/{bot.quote}")
    print(f"Wallet:  {bot.wallet_address}")
    print(f"Níveis:  {len(bot.levels)}")
    print(f"Range:   [{float(bot.levels[0]):.4f}..{float(bot.levels[-1]):.4f}]")
    print(f"Ordem:   ${float(bot.order_size_quote):.2f} {bot.quote}")
    print(f"Fee/tx:  ${float(SOL_FEE_USD):.4f}")
    print(f"dry_run: {bot.dry_run}")
    r = bot.tick()
    print(f"tick (init): {len(r)} sinais")
    print("SMOKE OK")
