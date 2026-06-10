# ============================================================
#  trader_api.py — Trader Dashboard API  v2
# ============================================================

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
import sqlite3
import subprocess, re, time, os
from concurrent.futures import ThreadPoolExecutor
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

_SETTINGS_PATH  = Path("/opt/crypto_bsc/config/settings.yaml")
_CRYPTO_BSC_DB  = Path("/opt/crypto_bsc/data/crypto_bsc.db")
FLASH_ARB_LOG   = Path("/home/trader/flash-arb-base/logs/arb_bot.log")

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

app = FastAPI(title="Trader Dashboard API", version="2.0.1")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# ─── Auth middleware ────────────────────────────────────────────────────────────
from fastapi import Request
from fastapi.responses import JSONResponse

AUTH_EXEMPT = {"/health"}

@app.middleware("http")
async def check_api_key(request: Request, call_next):
    if request.url.path in AUTH_EXEMPT:
        return await call_next(request)
    key = request.headers.get("x-api-key", "")
    if key != API_KEY:
        return JSONResponse(status_code=403, content={"detail": "Forbidden"})
    return await call_next(request)

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

# ─── AUDIT HELPERS ────────────────────────────────────────────────────────────

_BOT_SERVICE: dict[str, str] = {
    "aave_base":        "liquidator-aave-base",
    "aave_polygon":     "liquidator-aave-polygon",
    "aave_arb":         "liquidator-aave-arb",
    "aave_op":          "liquidator-aave-op",
    "aave_avax":        "liquidator-aave-avax",
    "aave_scroll":      "liquidator-aave-scroll",
    "aave_linea":       "liquidator-aave-linea",
    "compound_base":    "liquidator-compound-base",
    "compound_polygon": "liquidator-compound-polygon",
    "compound_arb":     "liquidator-compound-arb",
    "compound_op":      "liquidator-compound-op",
    "morpho_base":      "liquidator-morpho-base",
    "morpho_polygon":   "liquidator-morpho-polygon",
    "morpho_arb":       "liquidator-morpho-arb",
    "moonwell_base":    "crypto_bsc",
    "ionic_base":       "crypto_bsc",
    "venus_bsc":        "crypto_bsc",
}

_BOT_WS: set[str] = {"aave_base", "aave_polygon"}

_BOT_LOG_FILTER: dict[str, str] = {
    "moonwell_base": "moonwell_liquidator_base_bot",
    "ionic_base":    "ionic_liquidator_base_bot",
    "venus_bsc":     "venus_liquidator_bsc_bot",
}


def _fetch_gas_for_chains(wallet: str) -> dict:
    """Returns {chain_id: {balance, symbol}} for all liquidation chains."""
    import urllib.request as _req, json as _j
    CHAINS_AUDIT = [
        (8453,   ["https://base-rpc.publicnode.com",        "https://mainnet.base.org"],             "ETH"),
        (42161,  ["https://arb1.arbitrum.io/rpc",           "https://arbitrum-one-rpc.publicnode.com"], "ETH"),
        (10,     ["https://op-pokt-nm.nodies.app",          "https://optimism.publicnode.com"],       "ETH"),
        (534352, ["https://rpc.scroll.io"],                                                            "ETH"),
        (59144,  ["https://rpc.linea.build"],                                                          "ETH"),
        (137,    ["https://rpc.ankr.com/polygon",           "https://1rpc.io/matic"],                 "POL"),
        (43114,  ["https://api.avax.network/ext/bc/C/rpc"],                                           "AVAX"),
        (56,     ["https://bsc-dataseed.binance.org/",      "https://bsc-dataseed1.defibit.io/"],     "BNB"),
    ]
    _H = {"Content-Type": "application/json", "Accept": "application/json"}

    def _fetch_one(chain_id, rpcs, symbol):
        payload = _j.dumps({"jsonrpc": "2.0", "method": "eth_getBalance",
                             "params": [wallet, "latest"], "id": 1}).encode()
        for rpc in rpcs:
            try:
                r = _req.Request(rpc, data=payload, headers=_H, method="POST")
                with _req.urlopen(r, timeout=5) as resp:
                    result = _j.loads(resp.read()).get("result", "0x0")
                return chain_id, {"balance": round(int(result, 16) / 1e18, 6), "symbol": symbol}
            except Exception:
                pass
        return chain_id, {"balance": None, "symbol": symbol}

    out: dict = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for cid, data in ex.map(lambda c: _fetch_one(*c), CHAINS_AUDIT):
            out[cid] = data
    return out


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

    # Solana grid: track open BUY orders per pair to compute PnL on SELL
    open_buys: dict[str, list[tuple[float, float, float]]] = {}  # pair -> [(price, size, fee)]

    SOL_PAT  = re.compile(r"\[DRY_RUN\] Solana GRID (BUY|SELL) (\S+/\S+) @ ~([\d.]+).*size=\$([\d.]+).*fee=\$([\d.]+)")
    BSC_PAT  = re.compile(r"GRID (BUY|SELL) @ n.vel ([\d.]+) \(pre.o ~([\d.]+)\)")
    PNL_PAT  = re.compile(r"GRID (BUY|SELL) (\S+/\S+) @ ~([\d.]+).*pnl=([\d.-]+)")

    for line in logs:
        # Solana grid with size+fee — calculate PnL from BUY→SELL pairs
        m = SOL_PAT.search(line)
        if m:
            side  = m.group(1)
            pair  = m.group(2)
            price = float(m.group(3))
            size  = float(m.group(4))
            fee   = float(m.group(5))
            pnl   = 0.0
            if side == "BUY":
                open_buys.setdefault(pair, []).append((price, size, fee))
            else:  # SELL — match with oldest open BUY
                buys = open_buys.get(pair, [])
                if buys:
                    buy_price, buy_size, buy_fee = buys.pop(0)
                    pnl = round(buy_size * (price - buy_price) / buy_price - buy_fee - fee, 4)
                    total_pnl += pnl
            trades.append({"side": side, "pair": pair, "price": price,
                           "pnl": pnl, "timestamp": line[:25]})
            continue

        # BSC WBNB grid (no PnL in logs)
        m2 = BSC_PAT.search(line)
        if m2:
            trades.append({"side": m2.group(1), "pair": "WBNB/USDT",
                           "price": float(m2.group(3)), "pnl": 0.0, "timestamp": line[:25]})
            continue

        # Explicit pnl= format
        m3 = PNL_PAT.search(line)
        if m3:
            pnl = float(m3.group(4))
            total_pnl += pnl
            trades.append({"side": m3.group(1), "pair": m3.group(2),
                           "price": float(m3.group(3)), "pnl": round(pnl, 4), "timestamp": line[:25]})

    return {"total_pnl": round(total_pnl, 4), "total_trades": len(trades), "recent_trades": trades[-20:]}

def parse_flash_arb_logs(lines: list[str]) -> dict:
    """Parseia logs de arb_bot.py: [OPP] e [PROFIT]."""
    opps   = []
    trades = []
    total_pnl   = 0.0
    eth_balance = None

    OPP_PAT    = re.compile(r"\[OPP\] ([^|]+?) \| spread=([\d.]+)% \| reverse=(True|False)")
    PROFIT_PAT = re.compile(r"\[PROFIT\] ([^|]+?) \| spread=([\d.]+)% \| profit≈\$([\d.]+) \| total=\$([\d.]+) \| tx=(\w+)")
    BAL_PAT    = re.compile(r"ETH balance: ([\d.]+) ETH")

    for line in lines:
        m = BAL_PAT.search(line)
        if m:
            eth_balance = float(m.group(1))

        m = OPP_PAT.search(line)
        if m:
            opps.append({
                "pair":      m.group(1).strip(),
                "spread":    float(m.group(2)),
                "reverse":   m.group(3) == "True",
                "timestamp": line[:19],
            })

        m = PROFIT_PAT.search(line)
        if m:
            profit    = float(m.group(3))
            total_pnl = float(m.group(4))
            trades.append({
                "pair":      m.group(1).strip(),
                "spread":    float(m.group(2)),
                "profit":    round(profit, 4),
                "total":     round(total_pnl, 4),
                "tx":        m.group(5),
                "timestamp": line[:19],
            })

    last_opp = opps[-1] if opps else None
    return {
        "eth_balance":            eth_balance,
        "total_pnl_usd":          round(total_pnl, 4),
        "trades_executed":        len(trades),
        "opportunities_detected": len(opps),
        "last_spread":            last_opp["spread"] if last_opp else None,
        "last_spread_pair":       last_opp["pair"]   if last_opp else None,
        "recent_trades":          trades[-20:],
        "recent_opps":            opps[-10:],
    }

# ============ ENDPOINTS ============

@app.get("/health")
def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}

@app.get("/api/system")
def get_system():
    services = [
        "autonomous-trader", "crypto_bsc", "ibc-gateway", "trader-dashboard",
        "tgbot-ibkr", "tgbot-sniper", "tgbot-grid", "tgbot-dca",
        "tgbot-funding", "hermes-gateway", "flash-arb-bot"
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

@app.get("/api/flash-arb")
def get_flash_arb():
    lines = []
    try:
        lines = FLASH_ARB_LOG.read_text().splitlines()
    except Exception:
        pass
    svc  = get_service_status("flash-arb-bot")
    data = parse_flash_arb_logs(lines)
    return {**data, "service_status": svc, "recent_logs": lines[-60:]}

@app.get("/api/logs/{service}")
def get_logs(service: str, hours: int = 6, lines: int = 200):
    allowed = ["autonomous-trader", "crypto_bsc", "ibc-gateway", "tgbot-sniper",
               "tgbot-grid", "tgbot-funding", "trader-dashboard", "hermes-gateway",
               "tgbot-ibkr", "tgbot-dca", "flash-arb-bot"]
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

def _query_liquidations(chain: str, limit: int) -> dict:
    """Lê oportunidades de liquidação da DB filtrando por chain."""
    empty = {
        "opportunities": [],
        "summary": {"total": 0, "executed": 0, "total_est_profit": 0.0, "best_profit": 0.0},
        "chain": chain,
        "timestamp": datetime.utcnow().isoformat(),
    }
    if not _CRYPTO_BSC_DB.exists():
        return empty
    try:
        conn = sqlite3.connect(str(_CRYPTO_BSC_DB), timeout=5)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        exists = cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='liquidation_opportunities'"
        ).fetchone()
        if not exists:
            conn.close()
            return empty

        # Verificar se a coluna chain existe (migração pode ainda não ter corrido)
        has_chain = any(
            row[1] == "chain"
            for row in cur.execute("PRAGMA table_info(liquidation_opportunities)").fetchall()
        )

        if has_chain:
            chain_filter = "AND (chain = ? OR chain IS NULL)" if chain == "base" else "AND chain = ?"
            rows = cur.execute(f"""
                SELECT ts, position_address, health_factor,
                       debt_amount_usd, collateral_amount_usd, liquidation_bonus_pct,
                       estimated_profit_usd, gas_cost_usd,
                       executed, dry_run, status, tx_hash
                FROM liquidation_opportunities
                WHERE ts = (
                    SELECT MAX(ts) FROM liquidation_opportunities t2
                    WHERE t2.position_address = liquidation_opportunities.position_address
                    AND t2.chain = liquidation_opportunities.chain
                )
                {chain_filter}
                ORDER BY health_factor ASC
                LIMIT ?
            """, (chain, min(limit, 200))).fetchall()

            agg = cur.execute(f"""
                SELECT COUNT(*),
                       SUM(CASE WHEN executed = 1 THEN 1 ELSE 0 END),
                       ROUND(SUM(estimated_profit_usd), 2),
                       ROUND(MAX(estimated_profit_usd), 2)
                FROM liquidation_opportunities
                WHERE health_factor < 1.0
                {chain_filter}
            """, (chain,)).fetchone()
            exec_agg = cur.execute(f"""
                SELECT ROUND(SUM(estimated_profit_usd), 2)
                FROM liquidation_opportunities
                WHERE executed = 1
                {chain_filter}
            """, (chain,)).fetchone()
        else:
            # Sem coluna chain: só devolve dados para Base (todos os registos são Base)
            if chain != "base":
                conn.close()
                return empty
            rows = cur.execute("""
                SELECT ts, position_address, health_factor,
                       debt_amount_usd, collateral_amount_usd, liquidation_bonus_pct,
                       estimated_profit_usd, gas_cost_usd,
                       executed, dry_run, status, tx_hash
                FROM liquidation_opportunities
                WHERE ts = (
                    SELECT MAX(ts) FROM liquidation_opportunities t2
                    WHERE t2.position_address = liquidation_opportunities.position_address
                )
                ORDER BY health_factor ASC
                LIMIT ?
            """, (min(limit, 200),)).fetchall()

            agg = cur.execute("""
                SELECT COUNT(*),
                       SUM(CASE WHEN executed = 1 THEN 1 ELSE 0 END),
                       ROUND(SUM(estimated_profit_usd), 2),
                       ROUND(MAX(estimated_profit_usd), 2)
                FROM liquidation_opportunities
                WHERE health_factor < 1.0
            """).fetchone()
            exec_agg = cur.execute("""
                SELECT ROUND(SUM(estimated_profit_usd), 2)
                FROM liquidation_opportunities
                WHERE executed = 1
            """).fetchone()

        conn.close()

        opps = [
            {
                "ts":               r["ts"],
                "position_address": r["position_address"],
                "health_factor":    round(r["health_factor"], 4),
                "debt_usd":         round(r["debt_amount_usd"], 2),
                "collateral_usd":   round(r["collateral_amount_usd"], 2),
                "bonus_pct":        round(r["liquidation_bonus_pct"], 1),
                "estimated_profit": round(r["estimated_profit_usd"], 4),
                "gas_usd":          round(r["gas_cost_usd"], 6),
                "executed":         bool(r["executed"]),
                "dry_run":          bool(r["dry_run"]),
                "status":           r["status"],
                "tx_hash":          r["tx_hash"],
            }
            for r in rows
        ]

        return {
            "opportunities": opps,
            "summary": {
                "total":            agg[0] or 0,
                "executed":         agg[1] or 0,
                "total_est_profit": agg[2] or 0.0,
                "best_profit":      agg[3] or 0.0,
                "executed_profit":  exec_agg[0] or 0.0,
            },
            "chain": chain,
            "timestamp": datetime.utcnow().isoformat(),
        }
    except Exception as exc:
        return {**empty, "error": str(exc)}


@app.get("/api/liquidations")
def get_liquidations(limit: int = 50):
    return _query_liquidations("base", limit)


@app.get("/api/liquidations/polygon")
def get_liquidations_polygon(limit: int = 50):
    return _query_liquidations("polygon", limit)


@app.get("/api/liquidations/avax")
def get_liquidations_avax(limit: int = 50):
    return _query_liquidations("avax", limit)


@app.get("/api/liquidations/arb")
def get_liquidations_arb(limit: int = 50):
    return _query_liquidations("arb", limit)


@app.get("/api/liquidations/op")
def get_liquidations_op(limit: int = 50):
    return _query_liquidations("op", limit)


@app.get("/api/liquidations/scroll")
def get_liquidations_scroll(limit: int = 50):
    return _query_liquidations("scroll", limit)


@app.get("/api/liquidations/linea")
def get_liquidations_linea(limit: int = 50):
    return _query_liquidations("linea", limit)


@app.get("/api/liquidations/compound_base")
def get_liquidations_compound_base(limit: int = 50):
    return _query_liquidations("compound_base", limit)


@app.get("/api/liquidations/morpho_base")
def get_liquidations_morpho_base(limit: int = 50):
    return _query_liquidations("morpho_base", limit)


@app.get("/api/liquidations/compound_polygon")
def get_liquidations_compound_polygon(limit: int = 50):
    return _query_liquidations("compound_polygon", limit)


@app.get("/api/liquidations/compound_arb")
def get_liquidations_compound_arb(limit: int = 50):
    return _query_liquidations("compound_arb", limit)


@app.get("/api/liquidations/compound_op")
def get_liquidations_compound_op(limit: int = 50):
    return _query_liquidations("compound_op", limit)


@app.get("/api/liquidations/morpho_polygon")
def get_liquidations_morpho_polygon(limit: int = 50):
    return _query_liquidations("morpho_polygon", limit)


@app.get("/api/liquidations/morpho_arb")
def get_liquidations_morpho_arb(limit: int = 50):
    return _query_liquidations("morpho_arb", limit)


@app.get("/api/gas")
def get_gas():
    import urllib.request, json as _json

    WALLET = "0xb6E646Fa7a4e1CE48510BD3bcD756c00CbDFD434"
    CHAINS = [
        ("base",    ["https://base-rpc.publicnode.com", "https://mainnet.base.org"],           "ETH"),
        ("arb",     ["https://arb1.arbitrum.io/rpc", "https://arbitrum-one-rpc.publicnode.com"], "ETH"),
        ("op",      ["https://op-pokt-nm.nodies.app", "https://optimism.publicnode.com"],      "ETH"),
        ("scroll",  ["https://rpc.scroll.io"],                                                  "ETH"),
        ("linea",   ["https://rpc.linea.build"],                                               "ETH"),
        ("polygon", ["https://rpc.ankr.com/polygon", "https://1rpc.io/matic"],                 "POL"),
        ("avax",    ["https://api.avax.network/ext/bc/C/rpc"],                                 "AVAX"),
    ]
    _HEADERS = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; trader-bot/1.0)",
    }

    def _fetch(chain_id, rpcs, symbol):
        payload = _json.dumps({
            "jsonrpc": "2.0", "method": "eth_getBalance",
            "params": [WALLET, "latest"], "id": 1,
        }).encode()
        last_err = "no rpc"
        for rpc in rpcs:
            try:
                req = urllib.request.Request(rpc, data=payload, headers=_HEADERS, method="POST")
                with urllib.request.urlopen(req, timeout=5) as r:
                    result = _json.loads(r.read()).get("result", "0x0")
                balance = int(result, 16) / 1e18
                return chain_id, {"balance": round(balance, 6), "symbol": symbol}
            except Exception as exc:
                last_err = str(exc)[:80]
        return chain_id, {"balance": None, "symbol": symbol, "error": last_err}

    results = {}
    with ThreadPoolExecutor(max_workers=7) as ex:
        for chain_id, data in ex.map(lambda c: _fetch(*c), CHAINS):
            results[chain_id] = data

    return {**results, "wallet": WALLET, "ts": datetime.utcnow().isoformat()}


@app.get("/api/health")
def get_health(full: bool = Query(False)):
    """Comprehensive health of all 17 liquidation bots. ?full=true adds per-bot audit data."""
    cfg      = _load_yaml(_SETTINGS_PATH)
    bots_cfg = cfg.get("bots", {})

    def _contract(cfg_key: str, env_key: str = "") -> str:
        if env_key:
            v = os.environ.get(env_key, "")
            if v:
                return v
        return bots_cfg.get(cfg_key, {}).get("flash_loan_contract", "")

    base_contract     = os.environ.get("FLASH_LOAN_CONTRACT_BASE", "0x843730A2114b8624a36B4D4956aDdc6005bc5c30")
    polygon_contract  = _contract("aave_liquidator_polygon",  "FLASH_LOAN_CONTRACT_POLYGON")
    arb_contract      = _contract("aave_liquidator_arb",      "FLASH_LOAN_CONTRACT_ARB")
    op_contract       = _contract("aave_liquidator_op",       "FLASH_LOAN_CONTRACT_OP")
    avax_contract     = _contract("aave_liquidator_avax",     "FLASH_LOAN_CONTRACT_AVAX")
    moonwell_contract = bots_cfg.get("moonwell_liquidator_base", {}).get("flash_loan_contract", "")

    def _bot(id, name, protocol, chain, chain_id, contract):
        live = bool(contract)
        return {
            "id": id, "name": name, "protocol": protocol,
            "chain": chain, "chain_id": chain_id,
            "dry_run": not live,
            "contract": contract or None,
            "status": "live" if live else "dry_run",
        }

    bots = [
        _bot("aave_base",        "Aave V3 Base",          "Aave V3",     "Base",      8453,  base_contract),
        _bot("aave_polygon",     "Aave V3 Polygon",       "Aave V3",     "Polygon",   137,   polygon_contract),
        _bot("aave_arb",         "Aave V3 Arbitrum",      "Aave V3",     "Arbitrum",  42161, arb_contract),
        _bot("aave_op",          "Aave V3 Optimism",      "Aave V3",     "Optimism",  10,    op_contract),
        _bot("aave_avax",        "Aave V3 Avalanche",     "Aave V3",     "Avalanche", 43114, avax_contract),
        _bot("aave_scroll",      "Aave V3 Scroll",        "Aave V3",     "Scroll",    534352,""),
        _bot("aave_linea",       "Aave V3 Linea",         "Aave V3",     "Linea",     59144, ""),
        _bot("compound_base",    "Compound V3 Base",      "Compound V3", "Base",      8453,  base_contract),
        _bot("compound_polygon", "Compound V3 Polygon",   "Compound V3", "Polygon",   137,   polygon_contract),
        _bot("compound_arb",     "Compound V3 Arbitrum",  "Compound V3", "Arbitrum",  42161, arb_contract),
        _bot("compound_op",      "Compound V3 Optimism",  "Compound V3", "Optimism",  10,    op_contract),
        _bot("morpho_base",      "Morpho Blue Base",      "Morpho Blue", "Base",      8453,  base_contract),
        _bot("morpho_polygon",   "Morpho Blue Polygon",   "Morpho Blue", "Polygon",   137,   polygon_contract),
        _bot("morpho_arb",       "Morpho Blue Arbitrum",  "Morpho Blue", "Arbitrum",  42161, arb_contract),
        _bot("moonwell_base",    "Moonwell Base",         "Moonwell",    "Base",      8453,  moonwell_contract),
        _bot("ionic_base",       "Ionic Base",            "Ionic",       "Base",      8453,  base_contract),
        _bot("venus_bsc",        "Venus BSC",             "Venus",       "BSC",       56,    ""),
    ]

    live_count = sum(1 for b in bots if not b["dry_run"])
    svc        = get_service_status("crypto_bsc")

    # ── full audit (only when ?full=true) ─────────────────────────────────────
    if full:
        _WALLET  = "0xb6E646Fa7a4e1CE48510BD3bcD756c00CbDFD434"
        TICK_RE  = re.compile(r"→ tick|tick —|Tick:|tick bloco=", re.IGNORECASE)
        unique_svcs = list(set(_BOT_SERVICE.values()))

        def _fetch_svc(s: str):
            status = subprocess.run(
                ["systemctl", "is-active", s],
                capture_output=True, text=True, timeout=5
            ).stdout.strip()
            return s, {
                "status":     status,
                "tick_lines": run_journalctl(s, since="1h",  lines=400),
                "err_lines":  run_journalctl(s, since="2h",  lines=3000),
            }

        svc_cache:    dict = {}
        gas_by_chain: dict = {}
        with ThreadPoolExecutor(max_workers=10) as ex:
            gas_fut  = ex.submit(_fetch_gas_for_chains, _WALLET)
            svc_futs = {ex.submit(_fetch_svc, s): s for s in unique_svcs}
            gas_by_chain = gas_fut.result(timeout=20)
            for fut in svc_futs:
                try:
                    k, v = fut.result(timeout=35)
                    svc_cache[k] = v
                except Exception:
                    pass

        for b in bots:
            bid = b["id"]
            sd  = svc_cache.get(_BOT_SERVICE.get(bid, ""), {})
            lf  = _BOT_LOG_FILTER.get(bid)

            last_tick = None
            for line in reversed(sd.get("tick_lines", [])):
                if lf and lf not in line:
                    continue
                if TICK_RE.search(line):
                    m = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
                    if m:
                        last_tick = m.group(1)
                    break

            e429 = econn = 0
            for line in sd.get("err_lines", []):
                if not line:
                    continue
                if lf and lf not in line:
                    continue
                if "429" in line:
                    e429 += 1
                elif "sem ligação" in line:
                    econn += 1

            b["audit"] = {
                "systemd_status":  sd.get("status", "unknown"),
                "connection_type": "websocket" if bid in _BOT_WS else "http_polling",
                "last_tick":       last_tick,
                "errors_2h":       {"http_429": e429, "no_connection": econn},
                "gas":             gas_by_chain.get(b["chain_id"], {}),
            }
    # ──────────────────────────────────────────────────────────────────────────

    return {
        "status":  "ok" if svc["active"] else "degraded",
        "service": svc["status"],
        "bots":    bots,
        "summary": {
            "total":   len(bots),
            "live":    live_count,
            "dry_run": len(bots) - live_count,
        },
        "timestamp": datetime.utcnow().isoformat(),
    }


@app.get("/api/speed")
def get_speed():
    """Measures per-bot RPC latency, tick intervals and block-position estimates."""
    import urllib.request as _req, json as _j, time as _t

    _H = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; trader-bot/1.0)",
    }
    _PAYLOAD = _j.dumps({"jsonrpc": "2.0", "method": "eth_blockNumber",
                          "params": [], "id": 1}).encode()

    # ref_ashburn / ref_aws = estimated latency (ms) from us-east-1 to each chain's infra
    _SC: dict[int, dict] = {
        8453:   {"name": "Base",      "rpcs": ["https://base-rpc.publicnode.com",        "https://mainnet.base.org"],              "block_ms": 2000, "ref_ashburn": 10,  "ref_aws": 8  },
        42161:  {"name": "Arbitrum",  "rpcs": ["https://arb1.arbitrum.io/rpc",            "https://arb.drpc.org"],                  "block_ms": 250,  "ref_ashburn": 15,  "ref_aws": 12 },
        10:     {"name": "Optimism",  "rpcs": ["https://mainnet.optimism.io",             "https://optimism.publicnode.com"],       "block_ms": 2000, "ref_ashburn": 40,  "ref_aws": 35 },
        534352: {"name": "Scroll",    "rpcs": ["https://rpc.scroll.io"],                                                            "block_ms": 3000, "ref_ashburn": 80,  "ref_aws": 75 },
        59144:  {"name": "Linea",     "rpcs": ["https://rpc.linea.build",                 "https://linea.drpc.org"],                "block_ms": 3000, "ref_ashburn": 90,  "ref_aws": 85 },
        137:    {"name": "Polygon",   "rpcs": ["https://rpc.ankr.com/polygon",            "https://polygon.drpc.org"],              "block_ms": 2000, "ref_ashburn": 20,  "ref_aws": 18 },
        43114:  {"name": "Avalanche", "rpcs": ["https://api.avax.network/ext/bc/C/rpc"],                                           "block_ms": 2000, "ref_ashburn": 30,  "ref_aws": 25 },
        56:     {"name": "BSC",       "rpcs": ["https://bsc-dataseed.binance.org/",       "https://bsc-dataseed1.defibit.io/"],     "block_ms": 3000, "ref_ashburn": 200, "ref_aws": 195},
    }

    def _measure_chain(chain_id: int) -> tuple:
        cc = _SC.get(chain_id, {})
        for rpc in cc.get("rpcs", []):
            samples = []
            for _ in range(2):
                try:
                    t0 = _t.perf_counter()
                    r = _req.Request(rpc, data=_PAYLOAD, headers=_H, method="POST")
                    with _req.urlopen(r, timeout=4) as resp:
                        resp.read()
                    samples.append(int((_t.perf_counter() - t0) * 1000))
                except Exception:
                    break
            if len(samples) >= 1:
                samples.sort()
                return chain_id, samples[len(samples) // 2], rpc.split("//")[1].split("/")[0]
        return chain_id, None, ""

    TICK_RE = re.compile(r"→ tick|tick —|Tick:|tick bloco=", re.IGNORECASE)

    def _tick_interval(bot_id: str) -> int | None:
        svc = _BOT_SERVICE.get(bot_id, "")
        lf  = _BOT_LOG_FILTER.get(bot_id)
        if not svc:
            return None
        lines = run_journalctl(svc, since="15min", lines=150)
        tss = []
        for line in lines:
            if lf and lf not in line:
                continue
            if TICK_RE.search(line):
                m = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
                if m:
                    try:
                        tss.append(datetime.fromisoformat(m.group(1)))
                    except Exception:
                        pass
        if len(tss) < 2:
            return None
        intervals = sorted(
            int((tss[i+1] - tss[i]).total_seconds() * 1000)
            for i in range(len(tss) - 1)
            if 0 < (tss[i+1] - tss[i]).total_seconds() < 120
        )
        return intervals[len(intervals) // 2] if intervals else None

    # Build minimal bots list
    cfg      = _load_yaml(_SETTINGS_PATH)
    bots_cfg = cfg.get("bots", {})

    def _bc(k, ek=""):
        if ek:
            v = os.environ.get(ek, "")
            if v: return v
        return bots_cfg.get(k, {}).get("flash_loan_contract", "")

    base_c = os.environ.get("FLASH_LOAN_CONTRACT_BASE", "0x843730A2114b8624a36B4D4956aDdc6005bc5c30")
    _BOTS = [
        ("aave_base",        "Aave V3 Base",         "Base",      8453),
        ("aave_polygon",     "Aave V3 Polygon",      "Polygon",   137),
        ("aave_arb",         "Aave V3 Arbitrum",     "Arbitrum",  42161),
        ("aave_op",          "Aave V3 Optimism",     "Optimism",  10),
        ("aave_avax",        "Aave V3 Avalanche",    "Avalanche", 43114),
        ("aave_scroll",      "Aave V3 Scroll",       "Scroll",    534352),
        ("aave_linea",       "Aave V3 Linea",        "Linea",     59144),
        ("compound_base",    "Compound V3 Base",     "Base",      8453),
        ("compound_polygon", "Compound V3 Polygon",  "Polygon",   137),
        ("compound_arb",     "Compound V3 Arbitrum", "Arbitrum",  42161),
        ("compound_op",      "Compound V3 Optimism", "Optimism",  10),
        ("morpho_base",      "Morpho Blue Base",     "Base",      8453),
        ("morpho_polygon",   "Morpho Blue Polygon",  "Polygon",   137),
        ("morpho_arb",       "Morpho Blue Arbitrum", "Arbitrum",  42161),
        ("moonwell_base",    "Moonwell Base",        "Base",      8453),
        ("ionic_base",       "Ionic Base",           "Base",      8453),
        ("venus_bsc",        "Venus BSC",            "BSC",       56),
    ]

    unique_chains = list({cid for _, _, _, cid in _BOTS})

    lat_map: dict[int, tuple] = {}
    tick_map: dict[str, int | None] = {}

    with ThreadPoolExecutor(max_workers=12) as ex:
        chain_futs = {ex.submit(_measure_chain, cid): cid for cid in unique_chains}
        tick_futs  = {ex.submit(_tick_interval, bid): bid for bid, *_ in _BOTS}
        for fut, cid in chain_futs.items():
            try:
                _, lat, host = fut.result(timeout=15)
                lat_map[cid] = (lat, host)
            except Exception:
                lat_map[cid] = (None, "")
        for fut, bid in tick_futs.items():
            try:
                tick_map[bid] = fut.result(timeout=20)
            except Exception:
                tick_map[bid] = None

    rows = []
    for bid, name, chain, cid in _BOTS:
        cc       = _SC.get(cid, {})
        lat, host = lat_map.get(cid, (None, ""))
        tick     = tick_map.get(bid)
        pos      = round(lat / cc["block_ms"] * 100, 1) if lat and cc.get("block_ms") else None
        rows.append({
            "id":           bid,
            "name":         name,
            "chain":        chain,
            "chain_id":     cid,
            "connection":   "websocket" if bid in _BOT_WS else "http_polling",
            "rpc_latency":  lat,
            "rpc_host":     host,
            "tick_interval": tick,
            "block_ms":     cc.get("block_ms"),
            "position_pct": pos,
            "ref_ashburn":  cc.get("ref_ashburn"),
            "ref_aws":      cc.get("ref_aws"),
            "vs_ashburn":   (lat - cc["ref_ashburn"]) if lat is not None and cc.get("ref_ashburn") else None,
            "vs_aws":       (lat - cc["ref_aws"])     if lat is not None and cc.get("ref_aws")     else None,
        })

    # Sort by rpc_latency descending (None at the end), then by name
    rows.sort(key=lambda x: (x["rpc_latency"] is None, -(x["rpc_latency"] or 0), x["name"]))

    return {"bots": rows, "timestamp": datetime.utcnow().isoformat()}


# ─── OPPORTUNITIES ENDPOINT ───────────────────────────────────────────────────

_OUR_WALLET_LC = "0xb6e646fa7a4e1ce48510bd3bcd756c00cbdfd434"

_TOPIC_AAVE_LIQ   = "0xe413a321e8681d831f4dbccbca790d2952b56f977908e45be37335533e005286"
_TOPIC_MORPHO_LIQ = "0xa4946ede45d0c6f06a0f5ce92c9ad3b4751452d2fe0e25010783bcab57a67e41"
_TOPIC_CMPD_ABS   = "0x1547a878dc89ad3c367b6338b4be6a65a5dd74fb77ae044da1e8747ef1f4f62f"

_TOKEN_META: dict[str, tuple] = {
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": ("USDC",   6),
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca": ("USDbC",  6),
    "0x50c5725949a6f0c72e6c4a641f24049a917db0cb": ("DAI",   18),
    "0xfde4c96c8593536e31f229ea8f37b2ada2699bb2": ("USDT",   6),
    "0xaf88d065e77c8cc2239327c5edb3a432268e5831": ("USDC",   6),
    "0xff970a61a04b1ca14834a43f5de4533ebddb5cc8": ("USDC.e", 6),
    "0xda10009cbd5d07dd0cecc66161fc93d7c9000da1": ("DAI",   18),
    "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9": ("USDT",   6),
    "0x7f5c764cbc14f9669b88837ca1490cca17c31607": ("USDC.e", 6),
    "0x0b2c639c533813f4aa9d7837caf62653d097ff85": ("USDC",   6),
    "0x94b008aa00579c1307b0ef2c499ad98a8ce58e58": ("USDT",   6),
    "0x2791bca1f2de4661ed88a30c99a7a9449aa84174": ("USDC.e", 6),
    "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359": ("USDC",   6),
    "0xc2132d05d31c914a87c6611c10748aeb04b58e8f": ("USDT",   6),
    "0x4200000000000000000000000000000000000006": ("WETH",  18),
    "0x82af49447d8a07e3bd95bd0d56f35241523fbab1": ("WETH",  18),
    "0x7ceb23fd6bc0add59e62ac25578270cff1b9f619": ("WETH",  18),
    "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf": ("cbBTC",  8),
    "0x2ae3f1ec7f1f5012cfeab0185bfc7aa3cf0dec22": ("cbETH", 18),
    "0xc1cba3fcea344f92d9239c08c0568f6f2f0ee452": ("wstETH",18),
}
_STABLES = {"USDC", "USDbC", "USDT", "DAI", "USDC.e"}

# (protocol, chain_label, chain_id, rpc_url, contract, topic, chunk_blocks, day_blocks)
_SCAN_PROTOCOLS = [
    ("Aave V3",     "Base",     8453,  "https://mainnet.base.org",               "0xA238Dd80C259a72e81d7e4664a9801593F98d1c5", _TOPIC_AAVE_LIQ,    5000,  43200),
    ("Aave V3",     "Arbitrum", 42161, "https://arb1.arbitrum.io/rpc",           "0x794a61358D6845594F94dc1DB02A252b5b4814aD", _TOPIC_AAVE_LIQ,   50000, 345600),
    ("Aave V3",     "Optimism", 10,    "https://mainnet.optimism.io",            "0x794a61358D6845594F94dc1DB02A252b5b4814aD", _TOPIC_AAVE_LIQ,    5000,  43200),
    ("Aave V3",     "Polygon",  137,   "https://rpc-mainnet.matic.quiknode.pro", "0x794a61358D6845594F94dc1DB02A252b5b4814aD", _TOPIC_AAVE_LIQ,   43200,  43200),
    ("Morpho Blue", "Base",     8453,  "https://mainnet.base.org",               "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb", _TOPIC_MORPHO_LIQ,  5000,  43200),
    ("Morpho Blue", "Arbitrum", 42161, "https://arb1.arbitrum.io/rpc",           "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb", _TOPIC_MORPHO_LIQ, 50000, 345600),
    ("Compound V3", "Base",     8453,  "https://mainnet.base.org",               "0xb125E6687d4313864e53df431d5425969c15Eb2F", _TOPIC_CMPD_ABS,    5000,  43200),
    ("Compound V3", "Arbitrum", 42161, "https://arb1.arbitrum.io/rpc",           "0xA5EDBDD9646f8dFF606d7448e414884C7d905dCA", _TOPIC_CMPD_ABS,   50000, 345600),
    ("Compound V3", "Arbitrum", 42161, "https://arb1.arbitrum.io/rpc",           "0xd98Be00b5D27fc98112BdE293e487f8D4cA57d07", _TOPIC_CMPD_ABS,   50000, 345600),
    ("Compound V3", "Optimism", 10,    "https://mainnet.optimism.io",            "0x2e44e174f7D53F0212823acC11C01A11d58c5bCB", _TOPIC_CMPD_ABS,    5000,  43200),
    ("Compound V3", "Optimism", 10,    "https://mainnet.optimism.io",            "0x995E394b8B2437aC8Ce61Ee0bC610D617962B214", _TOPIC_CMPD_ABS,    5000,  43200),
    ("Compound V3", "Polygon",  137,   "https://rpc-mainnet.matic.quiknode.pro", "0xF25212E676D1F7F89Cd72fFEe66158f541246445", _TOPIC_CMPD_ABS,   43200,  43200),
    ("Compound V3", "Polygon",  137,   "https://rpc-mainnet.matic.quiknode.pro", "0xaeB318360f27748Acb200CE616E389A6C9409a07", _TOPIC_CMPD_ABS,   43200,  43200),
]

_OPP_CACHE: dict = {}  # window → {"data": ..., "ts": float}


def _opp_rpc(rpc_url: str, method: str, params: list) -> dict:
    import urllib.request as _u, json as _j
    req = _u.Request(rpc_url,
        data=_j.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1}).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "TraderDashboard/2"},
        method="POST")
    try:
        with _u.urlopen(req, timeout=15) as r:
            return _j.loads(r.read())
    except Exception as exc:
        return {"error": str(exc)}


def _eth_price_usd() -> float:
    import urllib.request as _u, json as _j
    cache = getattr(_eth_price_usd, "_c", (0.0, 0.0))
    if time.time() - cache[1] < 300:
        return cache[0]
    for url, key in [
        ("https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd", ("ethereum", "usd")),
        ("https://min-api.cryptocompare.com/data/price?fsym=ETH&tsyms=USD", ("USD",)),
    ]:
        try:
            req = _u.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with _u.urlopen(req, timeout=5) as r:
                d = _j.loads(r.read())
                price = d[key[0]][key[1]] if len(key) == 2 else d[key[0]]
                _eth_price_usd._c = (float(price), time.time())
                return float(price)
        except Exception:
            pass
    return 3000.0


def _token_usd(raw: int, addr: str, eth_px: float) -> float | None:
    meta = _TOKEN_META.get(addr.lower())
    if not meta:
        return None
    sym, dec = meta
    amt = raw / (10 ** dec)
    if sym in _STABLES:
        return round(amt, 2)
    if sym in ("WETH", "cbETH", "wstETH"):
        return round(amt * eth_px, 2)
    if sym == "cbBTC":
        return round(amt * 100_000, 2)
    return None


def _morpho_debt_usd(raw: int, eth_px: float) -> tuple[float | None, str]:
    """Heuristic USD estimate for Morpho repaidAssets (token unknown)."""
    if raw > 10 ** 15:
        return round(raw / 10**18 * eth_px, 2), "WETH~"
    if raw < 10 ** 12:
        return round(raw / 10**6, 2), "USDC~"
    return None, "?"


def _chunked_logs(rpc_url: str, from_b: int, to_b: int, chunk: int,
                  addr: str, topic: str) -> list:
    ranges = [(from_b + i, min(from_b + i + chunk - 1, to_b))
               for i in range(0, max(to_b - from_b, 1), chunk)]
    def _fetch(fb_tb: tuple) -> list:
        fb, tb = fb_tb
        r = _opp_rpc(rpc_url, "eth_getLogs", [{"fromBlock": hex(fb), "toBlock": hex(tb),
            "address": addr, "topics": [topic]}])
        return r.get("result") or []
    with ThreadPoolExecutor(max_workers=6) as ex:
        results: list = []
        for evts in ex.map(_fetch, ranges):
            results.extend(evts)
    return results


def _scan_protocol(proto: tuple, days: int, eth_px: float) -> list[dict]:
    protocol, chain, chain_id, rpc_url, contract, topic, chunk, day_blocks = proto
    total_blocks = day_blocks * days

    bn_res = _opp_rpc(rpc_url, "eth_blockNumber", [])
    if "error" in bn_res or "result" not in bn_res:
        return []
    try:
        cur_block = int(bn_res["result"], 16)
    except Exception:
        return []

    from_b = cur_block - total_blocks
    logs = _chunked_logs(rpc_url, from_b, cur_block, chunk, contract, topic)

    events: list[dict] = []
    for log in logs:
        try:
            if topic == _TOPIC_AAVE_LIQ:
                data = log["data"][2:]
                debt_raw   = int(data[0:64], 16)
                liquidator = "0x" + data[152:192]
                debt_addr  = "0x" + log["topics"][2][26:]
                debt_usd   = _token_usd(debt_raw, debt_addr, eth_px)
                meta = _TOKEN_META.get(debt_addr.lower(), ("?", 0))
                debt_sym = meta[0]
                profit_est = round(debt_usd * 0.05, 2) if debt_usd else None

            elif topic == _TOPIC_MORPHO_LIQ:
                liquidator = "0x" + log["topics"][2][26:]
                data = log["data"][2:]
                repaid_raw = int(data[0:64], 16)
                debt_usd, debt_sym = _morpho_debt_usd(repaid_raw, eth_px)
                debt_raw   = repaid_raw
                profit_est = round(debt_usd * 0.05, 2) if debt_usd else None

            elif topic == _TOPIC_CMPD_ABS:
                liquidator = "0x" + log["topics"][1][26:]
                data = log["data"][2:]
                # basePaidOut (slot0), usdValue (slot1 × 10^8)
                usd_raw = int(data[64:128], 16)
                debt_usd = round(usd_raw / 10**8, 2) if usd_raw > 0 else None
                debt_raw = int(data[0:64], 16)
                debt_sym = "USD"
                profit_est = round(debt_usd * 0.05, 2) if debt_usd else None
            else:
                continue

            events.append({
                "protocol":    protocol,
                "chain":       chain,
                "chain_id":    chain_id,
                "debt_asset":  debt_sym,
                "debt_raw":    debt_raw,
                "debt_usd":    debt_usd,
                "profit_est":  profit_est,
                "liquidator":  liquidator,
                "by_us":       liquidator.lower() == _OUR_WALLET_LC,
                "tx_hash":     log["transactionHash"],
                "block":       int(log["blockNumber"], 16),
            })
        except Exception:
            continue
    return events


@app.get("/api/opportunities")
def get_opportunities(window: str = Query("1d")):
    allowed = {"1d": 1, "2d": 2, "7d": 7}
    days = allowed.get(window, 1)
    cache_entry = _OPP_CACHE.get(window, {})
    if cache_entry and time.time() - cache_entry.get("ts", 0) < 300:
        return cache_entry["data"]

    eth_px = _eth_price_usd()

    all_events: list[dict] = []
    with ThreadPoolExecutor(max_workers=13) as ex:
        futs = [ex.submit(_scan_protocol, p, days, eth_px) for p in _SCAN_PROTOCOLS]
        for fut in futs:
            try:
                all_events.extend(fut.result(timeout=40))
            except Exception:
                pass

    all_events.sort(key=lambda e: e["block"], reverse=True)

    total  = len(all_events)
    by_us  = [e for e in all_events if e["by_us"]]
    by_cmp = [e for e in all_events if not e["by_us"]]

    def _sum_profit(lst: list) -> float:
        return round(sum(e["profit_est"] for e in lst if e.get("profit_est") is not None), 2)

    result = {
        "window":   window,
        "eth_price": eth_px,
        "summary": {
            "total":            total,
            "by_us":            len(by_us),
            "by_competitor":    len(by_cmp),
            "profit_captured":  _sum_profit(by_us),
            "profit_lost":      _sum_profit(by_cmp),
        },
        "events":    all_events,
        "timestamp": datetime.utcnow().isoformat(),
    }
    _OPP_CACHE[window] = {"data": result, "ts": time.time()}
    return result


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
