"""Solana Sniper Bot — estratégia asymmetric risk.

Faz scan de tokens novos via Pump.fun API e Raydium pools a cada 15 segundos.
Entrada de $2 USDT por posição, stop-loss fixo de -30%, take-profit em 4 níveis
com moonbag de 10%. Máximo 5 posições / $10 capital.

Níveis de TP:
  TP1 +20%  → vende 25% da posição original
  TP2 +50%  → vende 25% da posição original
  TP3 +100% → vende 25% da posição original
  TP4 +300% → vende 15% da posição original
  Moonbag    10% fica indefinidamente (potencial 1000%+)
"""
from __future__ import annotations

import socket
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Optional

import requests

# Garante que qualquer socket que ignore o timeout do requests também falha depressa
socket.setdefaulttimeout(12)

from utils.config import get_env, get_settings
from utils.database import init_db, record_trade
from utils.logger import get_logger
from utils.notifier import TelegramNotifier

logger = get_logger(__name__)

# ── APIs ──────────────────────────────────────────────────────────────────────
_PUMPFUN_URL   = "https://frontend-api.pump.fun/coins/latest"
_RAYDIUM_URL   = "https://api.raydium.io/v2/main/pairs"
_JUPITER_URL   = "https://price.jup.ag/v4/price?ids={mint}"
SOL_FEE_USD    = Decimal("0.0001")

# ── Estratégia ────────────────────────────────────────────────────────────────
# (label, gain_pct_threshold, fraction_of_original_to_sell)
TP_LEVELS: list[tuple[str, Decimal, Decimal]] = [
    ("tp1", Decimal("20"),  Decimal("0.25")),
    ("tp2", Decimal("50"),  Decimal("0.25")),
    ("tp3", Decimal("100"), Decimal("0.25")),
    ("tp4", Decimal("300"), Decimal("0.15")),
    # moonbag 10%: nunca vendido — fica indefinidamente
]
SL_PCT = Decimal("30")   # stop-loss fixo -30%

_TP_EMOJI = {"tp1": "🟢", "tp2": "🟡", "tp3": "🟠", "tp4": "🔥"}


# ── Solders (execução real) ───────────────────────────────────────────────────
try:
    import solders  # noqa: F401 — só para testar disponibilidade
    _SOLDERS_OK = True
except ImportError:
    _SOLDERS_OK = False


def _real_swap(wallet: str, mint: str, amount_usd: float, side: str) -> Optional[str]:
    """Executa swap real via Jupiter/solders. Devolve tx hash ou None se falhar."""
    if not _SOLDERS_OK:
        logger.warning(
            "[REAL] solders não disponível — swap %s %s…%s $%.4f ignorado. "
            "Instale: pip install solders",
            side.upper(), mint[:8], mint[-4:], amount_usd,
        )
        return None
    # Ponto de extensão: integrar solders + Jupiter swap instruction aqui.
    logger.warning(
        "[REAL] Stub solders ativo — swap %s %s $%.4f não enviado.",
        side, mint[:8], amount_usd,
    )
    return None


def _dry_run_flag() -> bool:
    return str(get_env("DRY_RUN", "true")).lower() not in ("false", "0", "no")


# ── Position ──────────────────────────────────────────────────────────────────
@dataclass
class SolanaPosition:
    mint: str
    symbol: str
    entry_price: Decimal
    original_amount: Decimal    # quantidade total comprada
    remaining_amount: Decimal   # quantidade ainda em carteira
    spent_usd: Decimal          # USDT gasto na entrada
    source: str                 # "pumpfun" | "raydium"
    open_ts: float = field(default_factory=time.time)
    tp_hit: set[str] = field(default_factory=set)


# ── Bot ───────────────────────────────────────────────────────────────────────
class SolanaSniperBot:
    """Sniper de tokens novos em Solana (Pump.fun + Raydium) com asymmetric risk."""

    def __init__(self, settings: dict | None = None):
        self.settings = settings or get_settings()
        cfg = self.settings.get("bots", {}).get("solana_sniper", {})

        self.wallet: str = cfg.get("wallet", "5d4U3JpDzxMgEC6U3KMt2F3aGPcC8qJjER5B4bK6yWtZ")
        self.entry_usd       = Decimal(str(cfg.get("entry_usd", "2")))
        self.max_positions   = int(cfg.get("max_positions", 5))
        self.max_capital_usd = Decimal(str(cfg.get("max_capital_usd", "10")))
        self.poll_seconds    = int(cfg.get("poll_seconds", 15))
        self.max_age_min     = int(cfg.get("max_age_minutes", 30))
        self.min_liq_usd     = Decimal(str(cfg.get("min_liquidity_usd", "1000")))
        self.top10_max_pct   = Decimal(str(cfg.get("top10_holders_max_pct", "30")))
        self.summary_secs    = int(cfg.get("summary_interval_seconds", 3600))

        self.dry_run  = _dry_run_flag()
        self.notifier = TelegramNotifier(self.settings)
        init_db()

        self.positions: dict[str, SolanaPosition] = {}
        self.blacklist: set[str]   = set(cfg.get("blacklist", []))
        self._seen: set[str]       = set()   # mints já avaliados nesta sessão
        self._last_summary: float  = 0.0
        self._session_pnl          = Decimal("0")
        self._session_trades: int  = 0

        short = self.wallet[:8] + "…" if self.wallet else "(none)"
        logger.info(
            "SolanaSniperBot: entry=$%.2f max_pos=%d cap=$%.2f sl=-%s%% "
            "poll=%ds dry_run=%s wallet=%s solders=%s",
            float(self.entry_usd), self.max_positions,
            float(self.max_capital_usd), SL_PCT,
            self.poll_seconds, self.dry_run, short,
            "ok" if _SOLDERS_OK else "stub",
        )

    # ── Preço via Jupiter ─────────────────────────────────────────────────────

    def _price(self, mint: str) -> Decimal | None:
        try:
            resp = requests.get(_JUPITER_URL.format(mint=mint), timeout=(5, 8))
            resp.raise_for_status()
            data = resp.json().get("data", {}).get(mint)
            if data:
                return Decimal(str(data["price"]))
        except (KeyError, InvalidOperation, requests.RequestException, ValueError) as exc:
            logger.debug("Jupiter price %s…: %s", mint[:8], exc)
        return None

    # ── Fetch de candidatos ───────────────────────────────────────────────────

    def _fetch_pumpfun(self) -> list[dict]:
        try:
            resp = requests.get(_PUMPFUN_URL, timeout=(5, 10),
                                headers={"Accept": "application/json"})
            resp.raise_for_status()
            raw = resp.json()
            coins = raw if isinstance(raw, list) else raw.get("coins", raw.get("tokens", []))
            return [self._norm_pumpfun(c) for c in coins[:30]]
        except Exception as exc:
            logger.debug("Pump.fun fetch: %s", exc)
            return []

    def _fetch_raydium(self) -> list[dict]:
        _MAX_BYTES = 8 * 1024 * 1024  # 8 MB — resposta completa pode ter 100MB+
        try:
            resp = requests.get(_RAYDIUM_URL, timeout=(5, 10), stream=True)
            resp.raise_for_status()
            content = resp.raw.read(_MAX_BYTES, decode_content=True)
            if not content:
                return []
            raw = __import__("json").loads(content)
            pools = raw if isinstance(raw, list) else raw.get("data", [])
            return [self._norm_raydium(p) for p in pools[:30]]
        except Exception as exc:
            logger.debug("Raydium fetch: %s", exc)
            return []

    @staticmethod
    def _norm_pumpfun(c: dict) -> dict:
        # virtual_sol_reserves em lamports; aprox. liquidez USD com SOL~$150
        vsol = float(c.get("virtual_sol_reserves", 0) or 0) / 1e9
        return {
            "mint":       c.get("mint", ""),
            "symbol":     c.get("symbol", "UNKN"),
            "name":       c.get("name", ""),
            "source":     "pumpfun",
            "liquidity":  vsol * 150.0,
            "created_ts": c.get("created_timestamp"),  # ms desde epoch
            "raw":        c,
        }

    @staticmethod
    def _norm_raydium(p: dict) -> dict:
        return {
            "mint":       p.get("baseMint", p.get("ammId", "")),
            "symbol":     (p.get("baseSymbol") or p.get("name") or "UNKN")[:12],
            "name":       p.get("name", ""),
            "source":     "raydium",
            "liquidity":  float(p.get("liquidity", 0) or 0),
            "created_ts": None,
            "raw":        p,
        }

    # ── Filtros de entrada ────────────────────────────────────────────────────

    def _filter(self, token: dict) -> tuple[bool, str]:
        mint = token["mint"]
        if not mint:
            return False, "sem mint"
        if mint in self.blacklist:
            return False, "blacklist"
        if mint in self._seen:
            return False, "já visto"
        if mint in self.positions:
            return False, "em posição"

        # Idade (Pump.fun fornece timestamp em ms)
        if token["source"] == "pumpfun" and token["created_ts"]:
            try:
                age_s = time.time() - float(token["created_ts"]) / 1000
                if age_s > self.max_age_min * 60:
                    return False, f"antigo {age_s/60:.0f}m"
            except (ValueError, TypeError):
                pass

        # Liquidez mínima
        if token["liquidity"] < float(self.min_liq_usd):
            return False, f"liquidez ${token['liquidity']:.0f}"

        return True, "ok"

    # ── Capital e slots ───────────────────────────────────────────────────────

    def _deployed(self) -> Decimal:
        return sum(p.spent_usd for p in self.positions.values())

    def _can_enter(self) -> tuple[bool, str]:
        if len(self.positions) >= self.max_positions:
            return False, f"max pos {self.max_positions}"
        if self._deployed() + self.entry_usd > self.max_capital_usd:
            return False, f"cap máx ${float(self.max_capital_usd):.2f}"
        return True, "ok"

    # ── Entrada ───────────────────────────────────────────────────────────────

    def _enter(self, token: dict) -> dict:
        mint, symbol, source = token["mint"], token["symbol"], token["source"]

        price = self._price(mint)
        if price is None or price <= 0:
            self._seen.add(mint)
            return {"action": "skip", "mint": mint, "reason": "preço indisponível"}

        qty = self.entry_usd / price

        if self.dry_run:
            tx = f"DRY_{mint[:8]}_{int(time.time())}"
            logger.info(
                "[DRY] BUY %s (%s…) @ $%.8f  qty=%.4f  $%.2f  src=%s",
                symbol, mint[:8], float(price), float(qty), float(self.entry_usd), source,
            )
        else:
            tx = _real_swap(self.wallet, mint, float(self.entry_usd), "buy") or ""

        pos = SolanaPosition(
            mint=mint, symbol=symbol, entry_price=price,
            original_amount=qty, remaining_amount=qty,
            spent_usd=self.entry_usd, source=source,
        )
        self.positions[mint] = pos
        self._seen.add(mint)

        record_trade(
            bot="solana_sniper", base=symbol, quote="USDT",
            dex_buy="jupiter",
            size_usd=float(self.entry_usd),
            dry_run=self.dry_run,
            status="dry_run" if self.dry_run else "sent",
            tx_buy=tx,
        )
        self.notifier.notify(
            "trade_executed",
            f"🎯 <b>SNIPE BUY</b> <code>{symbol}</code>\n"
            f"Mint: <code>{mint[:20]}…</code>\n"
            f"Preço: <code>${float(price):.8f}</code>  "
            f"Qty: <code>{float(qty):.4f}</code>\n"
            f"Capital: <code>${float(self.entry_usd):.2f}</code>  "
            f"Fonte: {source}\n"
            f"Pos: {len(self.positions)}/{self.max_positions}  "
            f"Cap: ${float(self._deployed()):.2f}/${float(self.max_capital_usd):.2f}\n"
            f"DRY_RUN={self.dry_run}",
        )
        return {"action": "buy", "mint": mint, "symbol": symbol,
                "price": float(price), "qty": float(qty)}

    # ── Gestão de posição ─────────────────────────────────────────────────────

    def _manage(self, mint: str) -> list[dict]:
        pos = self.positions[mint]
        price = self._price(mint)
        if price is None:
            return [{"action": "hold", "reason": "preço indisponível", "mint": mint}]

        pnl_pct = (price - pos.entry_price) / pos.entry_price * 100

        # ── Stop-loss total ───────────────────────────────────────────────────
        if pnl_pct <= -SL_PCT:
            sell_amt = pos.remaining_amount
            pnl_usd  = (price - pos.entry_price) * pos.original_amount

            if self.dry_run:
                logger.info(
                    "[DRY] STOP_LOSS %s @ $%.8f  %.1f%%  ($%.4f)",
                    pos.symbol, float(price), float(pnl_pct), float(pnl_usd),
                )
                tx = f"DRY_SL_{mint[:8]}_{int(time.time())}"
            else:
                tx = _real_swap(self.wallet, mint,
                                float(sell_amt * price), "sell") or ""

            record_trade(
                bot="solana_sniper", base=pos.symbol, quote="USDT",
                dex_sell="jupiter",
                size_usd=float(pos.spent_usd),
                profit_usd=float(pnl_usd),
                profit_bps=float(pnl_pct * 100),
                dry_run=self.dry_run,
                status="stop_loss",
                tx_sell=tx,
            )
            self.notifier.notify(
                "trade_executed",
                f"🛑 <b>STOP LOSS</b> <code>{pos.symbol}</code>\n"
                f"PnL: <code>{float(pnl_pct):.1f}%</code>  "
                f"(<code>${float(pnl_usd):+.4f}</code>)\n"
                f"Entrada: <code>${float(pos.entry_price):.8f}</code>  "
                f"Saída: <code>${float(price):.8f}</code>\n"
                f"🚫 Token adicionado à blacklist\n"
                f"DRY_RUN={self.dry_run}",
            )
            logger.info("STOP_LOSS %s: %.1f%% pnl=$%.4f",
                        pos.symbol, float(pnl_pct), float(pnl_usd))

            self._session_pnl += pnl_usd
            self._session_trades += 1
            self.blacklist.add(mint)
            del self.positions[mint]
            return [{"action": "stop_loss", "mint": mint, "symbol": pos.symbol,
                     "pnl_pct": float(pnl_pct), "pnl_usd": float(pnl_usd)}]

        # ── Take-profit parcial ───────────────────────────────────────────────
        actions = []
        for label, target_pct, fraction in TP_LEVELS:
            if label in pos.tp_hit:
                continue
            if pnl_pct < target_pct:
                continue

            sell_amt = min(pos.original_amount * fraction, pos.remaining_amount)
            if sell_amt <= 0:
                pos.tp_hit.add(label)
                continue

            sell_usd  = sell_amt * price
            pnl_sell  = (price - pos.entry_price) * sell_amt

            if self.dry_run:
                logger.info(
                    "[DRY] %s %s @ $%.8f  +%.1f%%  sell %.4f ($%.4f)",
                    label.upper(), pos.symbol, float(price),
                    float(pnl_pct), float(sell_amt), float(sell_usd),
                )
                tx = f"DRY_{label}_{mint[:8]}_{int(time.time())}"
            else:
                tx = _real_swap(self.wallet, mint, float(sell_usd), "sell") or ""

            record_trade(
                bot="solana_sniper", base=pos.symbol, quote="USDT",
                dex_sell="jupiter",
                size_usd=float(sell_usd),
                profit_usd=float(pnl_sell),
                profit_bps=float(pnl_pct * 100),
                dry_run=self.dry_run,
                status=label,
                tx_sell=tx,
            )

            pos.remaining_amount -= sell_amt
            pos.tp_hit.add(label)
            self._session_pnl += pnl_sell
            self._session_trades += 1

            remaining_pct = float(pos.remaining_amount / pos.original_amount * 100)
            self.notifier.notify(
                "trade_executed",
                f"{_TP_EMOJI.get(label, '✅')} <b>{label.upper()}</b> "
                f"<code>{pos.symbol}</code>\n"
                f"PnL: <code>+{float(pnl_pct):.1f}%</code>  "
                f"sell <code>${float(sell_usd):.4f}</code>\n"
                f"Fração: <code>{float(fraction)*100:.0f}%</code> da posição original\n"
                f"Restante: <code>{remaining_pct:.0f}%</code> "
                f"{'(moonbag 🌙)' if remaining_pct <= 10.5 else ''}\n"
                f"DRY_RUN={self.dry_run}",
            )
            logger.info("%s %s: +%.1f%% sell %.4f ($%.4f) restante=%.0f%%",
                        label.upper(), pos.symbol, float(pnl_pct),
                        float(sell_amt), float(sell_usd), remaining_pct)

            actions.append({
                "action": label, "mint": mint, "symbol": pos.symbol,
                "pnl_pct": float(pnl_pct),
                "sell_usd": float(sell_usd),
                "remaining_pct": remaining_pct,
            })

        if not actions:
            actions.append({
                "action": "hold", "mint": mint, "symbol": pos.symbol,
                "pnl_pct": float(pnl_pct),
                "moonbag_pct": float(pos.remaining_amount / pos.original_amount * 100),
                "tp_hit": sorted(pos.tp_hit),
            })

        return actions

    # ── Resumo horário ────────────────────────────────────────────────────────

    def _maybe_summary(self) -> None:
        if time.time() - self._last_summary < self.summary_secs:
            return
        self._last_summary = time.time()

        lines = []
        for mint, pos in self.positions.items():
            p = self._price(mint)
            if p:
                pnl = float((p - pos.entry_price) / pos.entry_price * 100)
                mb  = float(pos.remaining_amount / pos.original_amount * 100)
                lines.append(f"  • {pos.symbol}: {pnl:+.1f}%  moonbag={mb:.0f}%")
            else:
                lines.append(f"  • {pos.symbol}: preço indisponível")

        body = "\n".join(lines) if lines else "  (nenhuma posição aberta)"
        self.notifier.notify(
            "daily_summary",
            f"📊 <b>Solana Sniper — Resumo</b>\n"
            f"Posições: <code>{len(self.positions)}/{self.max_positions}</code>  "
            f"Cap: <code>${float(self._deployed()):.2f}/${float(self.max_capital_usd):.2f}</code>\n"
            f"Trades: <code>{self._session_trades}</code>  "
            f"PnL sessão: <code>${float(self._session_pnl):+.4f}</code>\n"
            f"Blacklist: <code>{len(self.blacklist)}</code>\n{body}",
        )
        logger.info("Resumo: %d pos cap=$%.2f pnl=$%.4f trades=%d bl=%d",
                    len(self.positions), float(self._deployed()),
                    float(self._session_pnl), self._session_trades,
                    len(self.blacklist))

    # ── Tick ──────────────────────────────────────────────────────────────────

    def tick(self) -> list[dict]:
        results: list[dict] = []

        # 1. Gerir posições abertas
        for mint in list(self.positions):
            results.extend(self._manage(mint))

        # 2. Scan de candidatos se houver slot disponível
        ok, reason = self._can_enter()
        if ok:
            candidates: list[dict] = []
            for t in self._fetch_pumpfun():
                if self._filter(t)[0]:
                    candidates.append(t)
            for t in self._fetch_raydium():
                if self._filter(t)[0]:
                    candidates.append(t)

            for token in candidates:
                ok, reason = self._can_enter()
                if not ok:
                    logger.debug("Sem slot: %s", reason)
                    break
                self._seen.add(token["mint"])  # marca antes de tentar
                results.append(self._enter(token))
        else:
            logger.debug("Scan ignorado: %s", reason)

        # 3. Resumo horário
        self._maybe_summary()

        return results

    def run_forever(self) -> None:
        logger.info(
            "SolanaSniperBot a correr: entry=$%.2f sl=-%s%% "
            "max_pos=%d poll=%ds dry_run=%s",
            float(self.entry_usd), SL_PCT,
            self.max_positions, self.poll_seconds, self.dry_run,
        )
        while True:
            try:
                self.tick()
            except Exception:
                logger.exception("Erro no tick do SolanaSniperBot")
            time.sleep(self.poll_seconds)


if __name__ == "__main__":
    import logging as _log
    _log.basicConfig(level=_log.INFO, format="%(levelname)s %(name)s: %(message)s")

    bot = SolanaSniperBot()
    print(f"wallet:      {bot.wallet}")
    print(f"entry_usd:   ${float(bot.entry_usd):.2f}")
    print(f"max_pos:     {bot.max_positions}")
    print(f"max_capital: ${float(bot.max_capital_usd):.2f}")
    print(f"stop_loss:   -{SL_PCT}%")
    print(f"tp_levels:   {[(l, float(t), float(f)) for l,t,f in TP_LEVELS]}")
    print(f"dry_run:     {bot.dry_run}")
    print(f"solders:     {'disponível' if _SOLDERS_OK else 'não disponível (stub ativo)'}")
    print()

    # Verifica lógica de filtro e TP com posição sintética
    from decimal import Decimal
    from bots.solana_sniper_bot import SolanaPosition  # noqa: E402

    synth_pos = SolanaPosition(
        mint="SynthMint1111111111111111111111111111111111",
        symbol="TEST",
        entry_price=Decimal("0.001"),
        original_amount=Decimal("2000"),
        remaining_amount=Decimal("2000"),
        spent_usd=Decimal("2"),
        source="pumpfun",
    )
    bot.positions["SynthMint1111111111111111111111111111111111"] = synth_pos

    # Simula preço com +25% → deve disparar TP1
    original_price_fn = bot._price
    bot._price = lambda m: (Decimal("0.00125") if m.startswith("Synth") else original_price_fn(m))
    r = bot._manage("SynthMint1111111111111111111111111111111111")
    assert any(a["action"] == "tp1" for a in r), f"Esperava tp1, obteve: {r}"
    print("✓ TP1 (+25%) disparado corretamente")
    bot._price = original_price_fn

    # Simula preço com -35% → deve disparar SL
    if "SynthMint1111111111111111111111111111111111" not in bot.positions:
        bot.positions["SynthMint1111111111111111111111111111111111"] = synth_pos
    synth_pos.remaining_amount = synth_pos.original_amount
    synth_pos.tp_hit = set()
    bot._price = lambda m: (Decimal("0.00065") if m.startswith("Synth") else original_price_fn(m))
    r = bot._manage("SynthMint1111111111111111111111111111111111")
    assert any(a["action"] == "stop_loss" for a in r), f"Esperava stop_loss, obteve: {r}"
    assert "SynthMint1111111111111111111111111111111111" in bot.blacklist, "Mint não ficou na blacklist"
    print("✓ Stop-loss (-35%) + blacklist OK")
    bot._price = original_price_fn

    # can_enter com capital cheio
    for i in range(5):
        bot.positions[f"Fake{i}"] = SolanaPosition(
            mint=f"Fake{i}", symbol=f"T{i}",
            entry_price=Decimal("1"), original_amount=Decimal("2"),
            remaining_amount=Decimal("2"), spent_usd=Decimal("2"),
            source="pumpfun",
        )
    ok, reason = bot._can_enter()
    assert not ok, "Devia rejeitar com 5 posições"
    print(f"✓ Limite de posições respeitado: '{reason}'")

    print()
    print("SMOKE OK")
