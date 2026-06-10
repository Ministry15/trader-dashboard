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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
