"""REST API para o trader dashboard — porta 5000, exposta via Nginx em 80, Auth: Bearer.

Endpoints:
  GET /health               — sem auth, health check
  GET /api/status           — estado do serviço e configuração
  GET /api/bots             — P&L e actividade por bot (DB)
  GET /api/trades           — últimos trades (?limit=50&bot=grid)
  GET /api/vps              — métricas do VPS (CPU, RAM, disco, serviços)
"""
from __future__ import annotations

import datetime
import subprocess
import sys
import time

sys.path.insert(0, "/opt/crypto_bsc")

from flask import Flask, jsonify, request
from flask_cors import CORS
from sqlalchemy import text

from utils.config import get_env, get_settings
from utils.database import get_session, init_db

API_TOKEN = "JPxK9m2026TraderB0t!"

app = Flask(__name__)
CORS(app, resources={"/*": {"origins": "*"}})

# ── Auth ──────────────────────────────────────────────────────────────────────

@app.before_request
def _check_auth():
    if request.path in ("/health", "/"):
        return
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {API_TOKEN}":
        return jsonify({"error": "Forbidden"}), 403

# ── Helpers ───────────────────────────────────────────────────────────────────

def _utc() -> str:
    return datetime.datetime.utcnow().isoformat() + "Z"

def _svc_status(name: str) -> str:
    try:
        r = subprocess.run(
            ["systemctl", "is-active", name],
            capture_output=True, text=True, timeout=3,
        )
        return r.stdout.strip()
    except Exception:
        return "unknown"

def _dry_run() -> bool:
    return str(get_env("DRY_RUN", "true")).lower() not in ("false", "0", "no")

def _fmt_uptime(secs: float) -> str:
    s = int(secs)
    d, r = divmod(s, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"

# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return jsonify({"status": "ok", "ts": _utc()})

@app.get("/")
def root():
    return jsonify({"service": "crypto_bsc API", "version": "1.0", "ts": _utc()})

@app.get("/api/status")
def api_status():
    settings = get_settings()
    enabled = settings.get("orchestrator", {}).get("enabled_bots", [])
    svc = _svc_status("crypto_bsc")

    # uptime since
    try:
        r = subprocess.run(
            ["systemctl", "show", "crypto_bsc", "--property=ActiveEnterTimestamp"],
            capture_output=True, text=True, timeout=3,
        )
        uptime_since = r.stdout.strip().replace("ActiveEnterTimestamp=", "")
    except Exception:
        uptime_since = ""

    return jsonify({
        "dry_run": _dry_run(),
        "enabled_bots": enabled,
        "crypto_bsc": svc,
        "uptime_since": uptime_since,
        "ts": _utc(),
    })


@app.get("/api/bots")
def api_bots():
    """Agrega P&L e actividade por bot a partir da base de dados."""
    BOT_META = {
        "grid":          {"display": "WBNB Grid",     "pair": "WBNB/USDT",  "chain": "BSC"},
        "pepe_grid":     {"display": "PEPE Grid",     "pair": "PEPE/USDT",  "chain": "BSC"},
        "solana_grid":   {"display": "SOL Grid",      "pair": "SOL/USDC",   "chain": "Solana"},
        "sniper":        {"display": "BSC Sniper",    "pair": "Any Token",  "chain": "BSC"},
        "funding_rate":  {"display": "Funding Rate",  "pair": "DOGE/USDT",  "chain": "CEX"},
        "dca":           {"display": "DCA",           "pair": "WBNB/USDT",  "chain": "BSC"},
        "arbitrage":     {"display": "Arbitrage",     "pair": "Multi-DEX",  "chain": "BSC"},
        "cex_grid":      {"display": "CEX Grid",      "pair": "DOGE/USDT",  "chain": "CEX"},
        "solana_sniper": {"display": "SOL Sniper",    "pair": "Any Token",  "chain": "Solana"},
    }

    with get_session() as s:
        rows = s.execute(text("""
            SELECT
                bot,
                count(*)                                              AS trades_total,
                round(sum(profit_usd), 4)                             AS pnl_total,
                round(sum(size_usd), 2)                               AS volume_total,
                max(ts)                                               AS last_trade,
                count(CASE WHEN ts > datetime('now','-1 day')  THEN 1 END) AS trades_24h,
                round(sum(CASE WHEN ts > datetime('now','-1 day')
                               THEN profit_usd ELSE 0 END), 4)        AS pnl_24h,
                count(CASE WHEN ts > datetime('now','-1 hour') THEN 1 END) AS trades_1h
            FROM trades
            GROUP BY bot
            ORDER BY trades_total DESC
        """)).fetchall()

        bots = []
        total_trades = 0
        total_pnl = 0.0
        total_volume = 0.0
        for row in rows:
            bot_id = row[0]
            meta = BOT_META.get(bot_id, {"display": bot_id, "pair": "?", "chain": "?"})
            pnl = row[2] or 0.0
            vol = row[3] or 0.0
            total_trades += row[1]
            total_pnl += pnl
            total_volume += vol
            bots.append({
                "bot":          bot_id,
                "display":      meta["display"],
                "pair":         meta["pair"],
                "chain":        meta["chain"],
                "trades_total": row[1],
                "pnl_total":    pnl,
                "volume_total": vol,
                "last_trade":   row[4],
                "trades_24h":   row[5] or 0,
                "pnl_24h":      row[6] or 0.0,
                "trades_1h":    row[7] or 0,
            })

        enabled = get_settings().get("orchestrator", {}).get("enabled_bots", [])
        active_count = sum(
            1 for b in bots
            if b["last_trade"] and
               (datetime.datetime.utcnow() - datetime.datetime.fromisoformat(b["last_trade"])).total_seconds() < 900
        )

    return jsonify({
        "bots": bots,
        "summary": {
            "total_trades":  total_trades,
            "total_pnl":     round(total_pnl, 4),
            "total_volume":  round(total_volume, 2),
            "active_bots":   active_count,
            "enabled_bots":  len(enabled),
        },
        "ts": _utc(),
    })


@app.get("/api/trades")
def api_trades():
    limit = min(int(request.args.get("limit", 50)), 200)
    bot = request.args.get("bot")

    with get_session() as s:
        if bot:
            rows = s.execute(text("""
                SELECT id, ts, bot, base, quote, dex_buy, dex_sell,
                       size_usd, profit_usd, status, dry_run
                FROM trades
                WHERE bot = :bot
                ORDER BY ts DESC LIMIT :lim
            """), {"bot": bot, "lim": limit}).fetchall()
        else:
            rows = s.execute(text("""
                SELECT id, ts, bot, base, quote, dex_buy, dex_sell,
                       size_usd, profit_usd, status, dry_run
                FROM trades
                ORDER BY ts DESC LIMIT :lim
            """), {"lim": limit}).fetchall()

        trades = [
            {
                "id":         r[0],
                "ts":         r[1],
                "bot":        r[2],
                "base":       r[3],
                "quote":      r[4],
                "dex_buy":    r[5],
                "dex_sell":   r[6],
                "size_usd":   r[7],
                "profit_usd": r[8],
                "status":     r[9],
                "dry_run":    bool(r[10]),
            }
            for r in rows
        ]

    return jsonify({"trades": trades, "count": len(trades), "ts": _utc()})


@app.get("/api/vps")
def api_vps():
    # uptime
    with open("/proc/uptime") as f:
        uptime_secs = float(f.read().split()[0])

    # memory
    mem = {}
    with open("/proc/meminfo") as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 2:
                mem[parts[0].rstrip(":")] = int(parts[1])
    mem_total_mb = mem.get("MemTotal", 0) // 1024
    mem_avail_mb = mem.get("MemAvailable", 0) // 1024
    mem_used_mb = mem_total_mb - mem_avail_mb

    # load avg
    with open("/proc/loadavg") as f:
        load = f.read().split()[:3]

    # cpu (0.15s sample)
    def _stat():
        with open("/proc/stat") as f:
            p = list(map(int, f.readline().split()[1:8]))
        return sum(p), p[3]
    t1, i1 = _stat()
    time.sleep(0.15)
    t2, i2 = _stat()
    dt = t2 - t1
    cpu_pct = round((1 - (i2 - i1) / dt) * 100, 1) if dt else 0.0

    # disk
    try:
        dk = subprocess.run(["df", "-h", "/"], capture_output=True, text=True, timeout=5)
        dl = dk.stdout.strip().split("\n")[1].split()
        disk = {"size": dl[1], "used": dl[2], "avail": dl[3],
                "pct": float(dl[4].replace("%", ""))}
    except Exception:
        disk = {"size": "?", "used": "?", "avail": "?", "pct": 0}

    # services
    svc_names = [
        "crypto_bsc", "autonomous-trader", "ibc-gateway",
        "trader-dashboard", "tgbot-ibkr", "tgbot-sniper",
    ]
    services = {name: _svc_status(name) for name in svc_names}

    return jsonify({
        "uptime_secs":   uptime_secs,
        "uptime_human":  _fmt_uptime(uptime_secs),
        "cpu_pct":       cpu_pct,
        "load_avg":      load,
        "mem_total_mb":  mem_total_mb,
        "mem_used_mb":   mem_used_mb,
        "mem_avail_mb":  mem_avail_mb,
        "mem_pct":       round(mem_used_mb / mem_total_mb * 100, 1) if mem_total_mb else 0,
        "disk":          disk,
        "services":      services,
        "ts":            _utc(),
    })


@app.get("/api/sniper")
def api_sniper():
    with get_session() as s:
        rows = s.execute(text("""
            SELECT ts, base, quote, profit_usd, status, dry_run
            FROM trades WHERE bot = 'sniper'
            ORDER BY ts DESC LIMIT 20
        """)).fetchall()

        stats = s.execute(text("""
            SELECT
                COUNT(*)                                                        AS total,
                SUM(CASE WHEN profit_usd > 0 THEN 1 ELSE 0 END)               AS wins,
                SUM(CASE WHEN profit_usd <= 0 AND profit_usd IS NOT NULL
                         THEN 1 ELSE 0 END)                                    AS losses,
                ROUND(SUM(profit_usd), 4)                                      AS total_pnl
            FROM trades WHERE bot = 'sniper'
        """)).fetchone()

        history = s.execute(text("""
            SELECT date(ts) AS day, ROUND(SUM(profit_usd), 4) AS pnl
            FROM trades
            WHERE bot = 'sniper' AND ts > datetime('now', '-7 days')
            GROUP BY day ORDER BY day
        """)).fetchall()

    total = stats[0] or 0
    wins  = stats[1] or 0
    losses = stats[2] or 0
    win_rate = round(wins / (wins + losses) * 100, 1) if (wins + losses) > 0 else 0.0

    return jsonify({
        "recent_trades": [
            {"token": r[1], "type": r[4], "pnl": r[3], "currency": r[2] or "USDT", "timestamp": r[0]}
            for r in rows
        ],
        "pnl_history": [
            {"date": r[0], "pnl_bnb": 0, "pnl_usdt": r[1] or 0}
            for r in history
        ],
        "total_pnl_bnb": 0,
        "total_pnl_usdt": stats[3] or 0.0,
        "win_rate": win_rate,
        "wins": wins,
        "losses": losses,
        "total_trades": total,
        "ts": _utc(),
    })


@app.get("/api/grid")
def api_grid():
    settings = get_settings()
    grid_bot_ids = ("grid", "pepe_grid", "solana_grid")

    with get_session() as s:
        recent = s.execute(text("""
            SELECT ts, bot, base, quote, profit_usd, status
            FROM trades
            WHERE bot IN ('grid', 'pepe_grid', 'solana_grid')
            ORDER BY ts DESC LIMIT 30
        """)).fetchall()

        agg = s.execute(text("""
            SELECT COUNT(*), ROUND(SUM(profit_usd), 4)
            FROM trades
            WHERE bot IN ('grid', 'pepe_grid', 'solana_grid')
        """)).fetchone()

    meta = {
        "grid":        {"pair": "WBNB/USDT", "chain": "BSC"},
        "pepe_grid":   {"pair": "PEPE/USDT", "chain": "BSC"},
        "solana_grid": {"pair": "SOL/USDC",  "chain": "Solana"},
    }
    active_grids = []
    for bot_id, m in meta.items():
        cfg = settings.get("bots", {}).get(bot_id, {})
        lower  = cfg.get("lower_price", "dynamic")
        upper  = cfg.get("upper_price", "dynamic")
        levels = cfg.get("grid_levels", cfg.get("grid_levels_n", "?"))
        active_grids.append({
            "bot":    bot_id,
            "pair":   m["pair"],
            "chain":  m["chain"],
            "price":  None,
            "lower":  lower,
            "upper":  upper,
            "range":  f"{lower}–{upper}" if lower != "dynamic" else "dynamic ±range",
            "levels": levels,
        })

    return jsonify({
        "active_grids": active_grids,
        "recent_trades": [
            {"pair": f"{r[2]}/{r[3]}", "side": r[5], "price": None, "pnl": r[4], "timestamp": r[0]}
            for r in recent
        ],
        "active_bots":   len(active_grids),
        "total_pnl":     agg[1] or 0.0,
        "total_trades":  agg[0] or 0,
        "ts": _utc(),
    })


@app.get("/api/pnl")
def api_pnl():
    with get_session() as s:
        today = s.execute(text("""
            SELECT
                bot,
                COUNT(*)                                           AS trades,
                ROUND(SUM(profit_usd), 4)                         AS pnl,
                SUM(CASE WHEN profit_usd > 0 THEN 1 ELSE 0 END)  AS wins,
                COUNT(*)                                           AS total
            FROM trades
            WHERE ts > datetime('now', 'start of day')
            GROUP BY bot
        """)).fetchall()

        history = s.execute(text("""
            SELECT
                date(ts)                                                                    AS day,
                ROUND(SUM(CASE WHEN bot='sniper' THEN profit_usd ELSE 0 END), 4)           AS sniper_pnl,
                ROUND(SUM(CASE WHEN bot IN ('grid','pepe_grid','solana_grid')
                               THEN profit_usd ELSE 0 END), 4)                             AS grid_pnl
            FROM trades
            WHERE ts > datetime('now', '-7 days')
            GROUP BY day ORDER BY day
        """)).fetchall()

    by_bot = {r[0]: r for r in today}
    sniper = by_bot.get("sniper")
    grid   = by_bot.get("grid")

    def _wr(row):
        if not row or not row[4]:
            return 0.0
        return round((row[3] or 0) / row[4] * 100, 1)

    return jsonify({
        "pnl": {
            "today": {
                "sniper_bnb":     0,
                "sniper_usdt":    sniper[2] if sniper else 0.0,
                "grid_usdt":      grid[2]   if grid   else 0.0,
                "sniper_trades":  sniper[1] if sniper else 0,
                "grid_trades":    grid[1]   if grid   else 0,
                "sniper_win_rate": _wr(sniper),
                "grid_win_rate":   _wr(grid),
            },
            "history_7d": [
                {"date": r[0], "sniper_bnb": 0, "sniper_usdt": r[1] or 0, "grid_usdt": r[2] or 0}
                for r in history
            ],
        },
        "ts": _utc(),
    })


@app.get("/api/funding")
def api_funding():
    with get_session() as s:
        rows = s.execute(text("""
            SELECT ts, base, quote, size_usd, profit_usd, status
            FROM trades WHERE bot = 'funding_rate'
            ORDER BY ts DESC LIMIT 50
        """)).fetchall()

        agg = s.execute(text("""
            SELECT COUNT(*), ROUND(SUM(profit_usd), 4)
            FROM trades WHERE bot = 'funding_rate'
        """)).fetchone()

    positions = []
    seen: set = set()
    for r in rows:
        sym = r[1]
        if sym not in seen:
            seen.add(sym)
            positions.append({
                "symbol":         sym,
                "side":           "LONG",
                "size":           r[3] or 0,
                "entry":          None,
                "mark":           None,
                "funding_earned": None,
                "unrealized_pnl": r[4] or 0,
            })

    return jsonify({
        "positions":          positions,
        "active_positions":   len(positions),
        "total_earned_usdt":  agg[1] or 0.0,
        "ts": _utc(),
    })


@app.get("/api/flash-arb")
def api_flash_arb():
    svc_active = _svc_status("flash-arb-base") == "active"

    with get_session() as s:
        rows = s.execute(text("""
            SELECT ts, base, quote, size_usd, profit_usd, status
            FROM trades WHERE bot = 'arbitrage'
            ORDER BY ts DESC LIMIT 20
        """)).fetchall()

        agg = s.execute(text("""
            SELECT COUNT(*), ROUND(SUM(profit_usd), 4)
            FROM trades WHERE bot = 'arbitrage'
        """)).fetchone()

    return jsonify({
        "service_status": {"active": svc_active, "status": "running" if svc_active else "stopped"},
        "recent_trades": [
            {"pair": f"{r[1]}/{r[2]}", "spread": None, "profit": r[4], "total": None,
             "tx": None, "timestamp": r[0]}
            for r in rows
        ],
        "recent_opps":           [],
        "recent_logs":           [],
        "total_pnl_usd":         agg[1] or 0.0,
        "trades_executed":       agg[0] or 0,
        "opportunities_detected": 0,
        "last_spread":           None,
        "last_spread_pair":      None,
        "eth_balance":           None,
        "ts": _utc(),
    })


@app.get("/api/ibkr")
def api_ibkr():
    return jsonify({
        "regime":  {"regime": "N/A", "vix": None, "spy": None},
        "signals": {"recent_signals": [], "signals_today": 0, "orders_placed": 0},
        "account": None,
        "mode":    "N/A",
        "ts": _utc(),
    })


@app.get("/api/system")
def api_system():
    with open("/proc/uptime") as f:
        uptime_secs = float(f.read().split()[0])

    mem = {}
    with open("/proc/meminfo") as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 2:
                mem[parts[0].rstrip(":")] = int(parts[1])
    mem_total_mb = mem.get("MemTotal", 0) // 1024
    mem_avail_mb = mem.get("MemAvailable", 0) // 1024
    mem_used_mb  = mem_total_mb - mem_avail_mb
    mem_pct      = round(mem_used_mb / mem_total_mb * 100, 1) if mem_total_mb else 0

    with open("/proc/loadavg") as f:
        load = f.read().split()[:3]

    def _stat():
        with open("/proc/stat") as f:
            p = list(map(int, f.readline().split()[1:8]))
        return sum(p), p[3]
    t1, i1 = _stat()
    time.sleep(0.15)
    t2, i2 = _stat()
    dt = t2 - t1
    cpu_pct = round((1 - (i2 - i1) / dt) * 100, 1) if dt else 0.0

    try:
        dk = subprocess.run(["df", "-h", "/"], capture_output=True, text=True, timeout=5)
        dl = dk.stdout.strip().split("\n")[1].split()
        disk_pct  = float(dl[4].replace("%", ""))
        disk_used = dl[2]
        disk_size = dl[1]
    except Exception:
        disk_pct, disk_used, disk_size = 0.0, "?", "?"

    svc_names = [
        "crypto_bsc", "crypto_bsc_api", "autonomous-trader",
        "ibc-gateway", "trader-dashboard", "tgbot-ibkr",
        "tgbot-sniper", "flash-arb-base",
    ]
    services = [
        {"service": n, "name": n, "status": _svc_status(n),
         "active": _svc_status(n) == "active"}
        for n in svc_names
    ]

    return jsonify({
        "cpu_percent":    cpu_pct,
        "memory_used_pct": mem_pct,
        "disk_used_pct":  disk_pct,
        "disk_used":      disk_used,
        "disk_size":      disk_size,
        "load_avg":       load,
        "uptime_secs":    uptime_secs,
        "services":       services,
        "ts": _utc(),
    })


@app.get("/api/logs/<service>")
def api_logs(service):
    allowed = {
        "crypto_bsc", "crypto_bsc_api", "autonomous-trader",
        "ibc-gateway", "trader-dashboard", "tgbot-ibkr",
        "tgbot-sniper", "tgbot-grid", "tgbot-funding",
        "hermes-gateway", "flash-arb-base", "flash-arb-bot",
    }
    if service not in allowed:
        return jsonify({"error": "unknown service"}), 400

    try:
        r = subprocess.run(
            ["journalctl", "-u", service, "-n", "100",
             "--no-pager", "--output=short-iso"],
            capture_output=True, text=True, timeout=10,
        )
        logs = r.stdout.strip().splitlines() if r.returncode == 0 else []
    except Exception as exc:
        logs = [f"[error reading logs: {exc}]"]

    return jsonify({"logs": logs, "service": service, "ts": _utc()})


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=False)
