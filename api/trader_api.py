# ============================================================
#  trader_api.py — Trader Dashboard API  v2
# ============================================================

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import subprocess, re, time, os
from datetime import datetime, timedelta
from pathlib import Path
try:
    import yaml as _yaml
    def _load_yaml(path):
        with open(path) as f:
            return _yaml.safe_load(f)
except ImportError:
    def _load_yaml(path):
        return {}

_SETTINGS_PATH = Path("/opt/crypto_bsc/config/settings.yaml")

def _grid_levels_from_config() -> dict:
    """Returns {pair: levels} read from settings.yaml bots section."""
    try:
        cfg = _load_yaml(_SETTINGS_PATH)
        bots = cfg.get("bots", {})
        result = {}
        for bot_cfg in bots.values():
            if not isinstance(bot_cfg, dict) or "grid_levels" not in bot_cfg:
                continue
            base  = bot_cfg.get("base", "")
            quote = bot_cfg.get("quote", "")
            if base and quote:
                result[f"{base}/{quote}"] = int(bot_cfg["grid_levels"])
        return result
    except Exception:
        return {}

API_KEY = "JPxK9m2026TraderB0t!"

app = FastAPI(title="Trader Dashboard API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# ============ HELPERS ============

def run_journalctl(service: str, since: str = "24h", lines: int = 500) -> list[str]:
    """Lê últimas N linhas de um serviço desde X tempo."""
    try:
        since_arg = since if since.endswith("ago") or "-" in since else f"{since} ago"
        r = subprocess.run(
            ["journalctl", "-u", service, "--since", since_arg,
             "--no-pager", "-o", "short-iso", "-n", str(lines)],
            capture_output=True, text=True, timeout=30
        )
        return r.stdout.strip().split("\n") if r.stdout.strip() else []
    except Exception:
        return []

def run_journalctl_grep(service: str, since: str, pattern: str) -> list[str]:
    """Filtra logs via grep em pipe — eficiente para volumes grandes (ex: 90k linhas)."""
    try:
        since_arg = since if since.endswith("ago") or "-" in since else f"{since} ago"
        j = subprocess.Popen(
            ["journalctl", "-u", service, "--since", since_arg,
             "--no-pager", "-o", "short-iso"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        g = subprocess.Popen(
            ["grep", "-i", pattern],
            stdin=j.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        j.stdout.close()
        out, _ = g.communicate(timeout=60)
        return out.decode("utf-8", errors="replace").strip().split("\n") if out.strip() else []
    except Exception:
        return []

def get_cpu_percent() -> float:
    try:
        def read_stat():
            with open("/proc/stat") as f:
                line = f.readline()
            parts = list(map(int, line.split()[1:8]))
            return sum(parts), parts[3]
        t1, i1 = read_stat()
        time.sleep(0.1)
        t2, i2 = read_stat()
        dt = t2 - t1
        return round((1 - (i2 - i1) / dt) * 100, 1) if dt else 0.0
    except Exception:
        return 0.0

def get_service_status(service: str) -> dict:
    try:
        r = subprocess.run(["systemctl", "is-active", service],
                           capture_output=True, text=True, timeout=5)
        status = r.stdout.strip()
        r2 = subprocess.run(["systemctl", "show", service, "--property=ActiveEnterTimestamp"],
                            capture_output=True, text=True, timeout=5)
        since = r2.stdout.strip().replace("ActiveEnterTimestamp=", "")
        return {"service": service, "status": status, "active": status == "active", "uptime_since": since}
    except Exception:
        return {"service": service, "status": "unknown", "active": False}

def parse_sniper_pnl(logs: list[str]) -> dict:
    """Parseia TAKE_PROFIT e STOP_LOSS em WBNB e USDT."""
    trades = []
    total_bnb = 0.0
    total_usdt = 0.0
    wins = losses = 0

    PAT = re.compile(r"SNIPE (TAKE_PROFIT|STOP_LOSS) (\S+): PnL ([+-]?[\d.]+) (WBNB|USDT)")
    for line in logs:
        m = PAT.search(line)
        if not m:
            continue
        result, token, pnl_str, currency = m.groups()
        pnl = float(pnl_str)
        if result == "TAKE_PROFIT":
            wins += 1
        else:
            losses += 1
            pnl = -abs(pnl)  # garantir negativo
        if currency == "WBNB":
            total_bnb += pnl
        else:
            total_usdt += pnl
        trades.append({
            "type": result,
            "side": "SELL",
            "token": token[:16] + ("…" if len(token) > 16 else ""),
            "pnl": round(pnl, 6),
            "currency": currency,
            "timestamp": line[:25],
        })

    win_rate = round(wins / (wins + losses) * 100, 1) if (wins + losses) > 0 else 0
    return {
        "total_pnl_bnb":  round(total_bnb, 6),
        "total_pnl_usdt": round(total_usdt, 4),
        "wins":           wins,
        "losses":         losses,
        "win_rate":       win_rate,
        "total_trades":   wins + losses,
        "recent_trades":  trades[-20:],
    }

def parse_ibkr_signals(logs: list[str]) -> dict:
    signals = []
    orders_placed = 0
    PAT = re.compile(r"SIGNAL \[(\w+)\] (\w+) (\w+) @ ([\d.]+) \| SL=([\d.]+) TP1=([\d.]+).*Score=(\d+)")
    for line in logs:
        m = PAT.search(line)
        if m:
            signals.append({
                "strategy":    m.group(1),
                "ticker":      m.group(2),
                "direction":   m.group(3),
                "price":       float(m.group(4)),
                "stop_loss":   float(m.group(5)),
                "take_profit": float(m.group(6)),
                "score":       int(m.group(7)),
                "timestamp":   line[:25],
            })
        if "orders placed" in line.lower() and "0 orders" not in line:
            pm = re.search(r"(\d+) orders placed", line)
            if pm:
                orders_placed += int(pm.group(1))
    return {"signals_today": len(signals), "orders_placed": orders_placed, "recent_signals": signals[-10:]}

def parse_regime(logs: list[str]) -> dict:
    PAT = re.compile(r"Regime detected: (\w+) \| SPY=([\d.]+).*VIX=([\d.]+)")
    for line in reversed(logs):
        m = PAT.search(line)
        if m:
            return {"regime": m.group(1), "spy": float(m.group(2)),
                    "vix": float(m.group(3)), "timestamp": line[:25]}
    return {"regime": "UNKNOWN", "spy": 0, "vix": 0}

def parse_grid_trades(logs: list[str]) -> dict:
    trades = []
    total_pnl = 0.0
    for line in logs:
        # WBNB/BSC grid
        m = re.search(r"GRID (BUY|SELL) @ n.vel ([\d.]+) \(pre.o ~([\d.]+)\)", line)
        if m:
            trades.append({"side": m.group(1), "pair": "WBNB/USDT",
                           "price": float(m.group(3)), "pnl": 0.0, "timestamp": line[:25]})
            continue
        # Solana grid dry_run
        m2 = re.search(r"\[DRY_RUN\] Solana GRID (BUY|SELL) (\S+) @ ~([\d.]+)", line)
        if m2:
            trades.append({"side": m2.group(1), "pair": m2.group(2),
                           "price": float(m2.group(3)), "pnl": 0.0, "timestamp": line[:25]})
            continue
        # pnl format
        m3 = re.search(r"GRID (BUY|SELL) (\S+/\S+) @ ~([\d.]+).*pnl=([\d.-]+)", line)
        if m3:
            pnl = float(m3.group(4))
            total_pnl += pnl
            trades.append({"side": m3.group(1), "pair": m3.group(2),
                           "price": float(m3.group(3)), "pnl": round(pnl, 4), "timestamp": line[:25]})
    return {"total_pnl": round(total_pnl, 4), "total_trades": len(trades), "recent_trades": trades[-20:]}

# ============ ENDPOINTS ============

@app.get("/health")
def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}

@app.get("/api/system")
def get_system():
    services = [
        "autonomous-trader", "crypto_bsc", "ibc-gateway", "trader-dashboard",
        "tgbot-ibkr", "tgbot-sniper", "tgbot-grid", "tgbot-dca",
        "tgbot-funding", "hermes-gateway"
    ]
    cpu_pct = get_cpu_percent()
    try:
        with open("/proc/loadavg") as f:
            load = f.read().split()[:3]
        with open("/proc/meminfo") as f:
            mems = f.readlines()
        mt = int(next(l for l in mems if "MemTotal" in l).split()[1])
        ma = int(next(l for l in mems if "MemAvailable" in l).split()[1])
        ram_pct = round((1 - ma / mt) * 100, 1)
        dk = subprocess.run(["df", "-h", "/"], capture_output=True, text=True).stdout
        dl = dk.strip().split("\n")[1].split()
        disk_pct = float(dl[4].replace("%", "")) if len(dl) > 4 else 0.0
    except Exception:
        load, ram_pct, dl, disk_pct = ["0","0","0"], 0, ["?","?","?","?","0%"], 0.0
    return {
        "services":        [get_service_status(s) for s in services],
        "cpu_percent":     cpu_pct,
        "load_avg":        load,
        "memory_used_pct": ram_pct,
        "disk_size":       dl[1] if len(dl) > 1 else "?",
        "disk_used":       dl[2] if len(dl) > 2 else "?",
        "disk_used_pct":   disk_pct,
        "timestamp":       datetime.utcnow().isoformat(),
    }

@app.get("/api/ibkr")
def get_ibkr(hours: int = 24):
    logs = []
    try:
        log_file = Path("/opt/autonomous_trader/logs") / f"trader_{datetime.utcnow().strftime('%Y-%m-%d')}.log"
        if log_file.exists():
            logs = log_file.read_text().splitlines()
    except Exception:
        pass
    jlogs = run_journalctl("autonomous-trader", f"{hours} hours", 500)
    all_logs = logs + jlogs
    regime  = parse_regime(all_logs)
    signals = parse_ibkr_signals(all_logs)
    pnl_by_day = {}
    for line in all_logs:
        m = re.search(r"daily.*pnl[:\s]+([\d.-]+)", line, re.IGNORECASE)
        if m:
            pnl_by_day[line[:10]] = float(m.group(1))
    return {
        "account": "DUQ447019", "mode": "PAPER",
        "regime": regime, "signals": signals,
        "pnl_by_day": pnl_by_day, "recent_logs": all_logs[-50:],
    }

@app.get("/api/sniper")
def get_sniper():
    # Piped grep — filtra 92k linhas para ~500 linhas de sniper em ~1.5s
    sniper_logs = run_journalctl_grep("crypto_bsc", "7 days", "sniper")
    pnl = parse_sniper_pnl(sniper_logs)

    pnl_history = []
    for i in range(6, -1, -1):
        day_str = (datetime.utcnow() - timedelta(days=i)).strftime("%Y-%m-%d")
        day_logs = [l for l in sniper_logs if day_str in l]
        dp = parse_sniper_pnl(day_logs)
        pnl_history.append({
            "date":       day_str,
            "pnl_bnb":    dp["total_pnl_bnb"],
            "pnl_usdt":   dp["total_pnl_usdt"],
            "trades":     dp["total_trades"],
            "win_rate":   dp["win_rate"],
        })

    return {**pnl, "pnl_history": pnl_history, "recent_logs": sniper_logs[-50:]}

@app.get("/api/grid")
def get_grid(hours: int = 24):
    logs = run_journalctl("crypto_bsc", f"{hours} hours", 500)
    grid_logs = [l for l in logs if "grid" in l.lower()]
    pnl = parse_grid_trades(grid_logs)

    # Grep para init messages (estão >1000 linhas atrás no journal)
    init_logs = run_journalctl_grep("crypto_bsc", f"{hours} hours", r"grid.*iniciada\|SolanaGridBot")

    active_grids = []
    seen = set()
    PAT_SOL  = re.compile(r"SolanaGridBot (\S+/\S+): pre.o=([\d.]+) range=([^\s]+) (\d+) n.veis \[([\d.]+)\.\.([\d.]+)\]")
    PAT_GRID = re.compile(r"Grid (\S+/\S+) iniciada\. Pre.o de refer.ncia: ([\d.]+) range \[([\d.]+)\.\.([\d.]+)\]")

    for line in reversed(init_logs):
        m = PAT_SOL.search(line)
        if m:
            pair = m.group(1)
            if pair not in seen:
                seen.add(pair)
                active_grids.append({
                    "bot": "SolanaGridBot", "pair": pair,
                    "price": float(m.group(2)), "range": m.group(3),
                    "levels": int(m.group(4)),
                    "lower": float(m.group(5)), "upper": float(m.group(6)),
                    "status": "active",
                })
            continue
        m2 = PAT_GRID.search(line)
        if m2:
            pair = m2.group(1)
            if pair not in seen:
                seen.add(pair)
                cfg_levels = _grid_levels_from_config()
                active_grids.append({
                    "bot": "GridBot", "pair": pair,
                    "price": float(m2.group(2)), "range": "dynamic",
                    "levels": cfg_levels.get(pair, 0),
                    "lower": float(m2.group(3)), "upper": float(m2.group(4)),
                    "status": "active",
                })
        if len(active_grids) >= 5:
            break

    return {**pnl, "active_bots": len(active_grids), "active_grids": active_grids, "recent_logs": grid_logs[-50:]}

@app.get("/api/funding")
def get_funding(hours: int = 24):
    logs = run_journalctl("crypto_bsc", f"{hours} hours", 500)
    funding_logs = [l for l in logs if "funding" in l.lower() or "cexgrid" in l.lower()]

    positions = []
    total_earned = 0.0
    for line in funding_logs:
        m = re.search(r"CexGrid (\S+): (\d+) n.veis \[([\d.]+)\.\.([\d.]+)\].*([\d.]+) USDT/ordem", line)
        if m:
            sym = m.group(1)
            if not any(p["symbol"] == sym for p in positions):
                positions.append({
                    "symbol": sym, "side": "long",
                    "size": float(m.group(5)), "entry": float(m.group(3)),
                    "mark": float(m.group(4)), "funding_rate": 0.0,
                    "funding_earned": 0.0, "unrealized_pnl": 0.0,
                })
        em = re.search(r"earned.*\+([\d.]+)", line, re.IGNORECASE)
        if em:
            total_earned += float(em.group(1))
    return {"active_positions": len(positions), "positions": positions,
            "total_earned_usdt": round(total_earned, 4), "recent_logs": funding_logs[-30:]}

@app.get("/api/logs/{service}")
def get_logs(service: str, hours: int = 6, lines: int = 200):
    allowed = ["autonomous-trader", "crypto_bsc", "ibc-gateway", "tgbot-sniper",
               "tgbot-grid", "tgbot-funding", "trader-dashboard", "hermes-gateway",
               "tgbot-ibkr", "tgbot-dca"]
    if service not in allowed:
        raise HTTPException(status_code=400, detail=f"Not allowed. Use: {allowed}")
    raw = run_journalctl(service, f"{hours}h", lines)
    parsed = []
    for line in raw:
        level = "INFO"
        if re.search(r"ERROR|CRITICAL|FATAL", line): level = "ERROR"
        elif re.search(r"WARNING|WARN", line): level = "WARN"
        elif re.search(r"\bDEBUG\b", line): level = "DEBUG"
        parsed.append({"raw": line, "level": level})
    return {"service": service, "hours": hours, "count": len(parsed), "logs": parsed}

@app.get("/api/pnl")
def get_pnl_summary():
    # 24h para hoje (limitado)
    logs_24h = run_journalctl("crypto_bsc", "24h", 2000)
    sniper_today = parse_sniper_pnl([l for l in logs_24h if "sniper" in l.lower()])
    grid_today   = parse_grid_trades([l for l in logs_24h if "grid" in l.lower()])

    # 7 dias — um único grep combinado sniper+grid
    combined_7d = run_journalctl_grep(
        "crypto_bsc", "7 days",
        r"TAKE_PROFIT\|STOP_LOSS\|GRID BUY\|GRID SELL"
    )
    sniper_7d = [l for l in combined_7d if "sniper" in l.lower()]
    grid_7d   = [l for l in combined_7d if "grid" in l.lower()]

    history = []
    for i in range(6, -1, -1):
        day_str = (datetime.utcnow() - timedelta(days=i)).strftime("%Y-%m-%d")
        ds = parse_sniper_pnl([l for l in sniper_7d if day_str in l])
        dg = parse_grid_trades([l for l in grid_7d if day_str in l])
        history.append({
            "date":        day_str,
            "sniper_bnb":  ds["total_pnl_bnb"],
            "sniper_usdt": ds["total_pnl_usdt"],
            "grid_usdt":   dg["total_pnl"],
            "ibkr_usd":    0,
        })

    return {
        "today": {
            "sniper_bnb":      sniper_today["total_pnl_bnb"],
            "sniper_usdt":     sniper_today["total_pnl_usdt"],
            "sniper_trades":   sniper_today["total_trades"],
            "sniper_win_rate": sniper_today["win_rate"],
            "grid_usdt":       grid_today["total_pnl"],
            "grid_trades":     grid_today["total_trades"],
        },
        "history_7d": history,
        "timestamp": datetime.utcnow().isoformat(),
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
