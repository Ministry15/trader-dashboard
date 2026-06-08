import React, { useState, useEffect, useCallback, useRef } from 'react';
import './App.css';
import {
  BarChart, Bar,
  XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, ReferenceLine
} from 'recharts';
import {
  Activity, TrendingUp, TrendingDown, Zap, Grid, DollarSign,
  Server, FileText, RefreshCw, AlertTriangle,
  CheckCircle, XCircle, Cpu, HardDrive, MemoryStick,
  BarChart2, Target, X, Download
} from 'lucide-react';

// ─── API ──────────────────────────────────────────────────────────────────────
async function apiFetch(endpoint) {
  const res = await fetch(`/api/proxy?path=${encodeURIComponent(endpoint)}`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

// ─── Helpers ──────────────────────────────────────────────────────────────────
const fmt    = (n, d = 2) => n == null ? '—' : Number(n).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
const fmtUSD = n => n == null ? '—' : (n < 0 ? '-$' : '$') + Math.abs(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmtBNB = n => n == null ? '—' : (n < 0 ? '-' : '') + Math.abs(Number(n)).toFixed(6) + ' BNB';
const pct    = n => n == null ? '—' : `${Number(n).toFixed(2)}%`;
const clr    = n => n == null ? '' : Number(n) >= 0 ? 'pos' : 'neg';

function Loader() {
  return <div className="loader"><span className="blink">_</span> loading...</div>;
}

function Err({ msg }) {
  return <div className="err-box">[ERROR] {msg}</div>;
}

function ChartTip({ active, payload, label }) {
  if (!active || !payload?.length) return null;
  return (
    <div className="chart-tip">
      <div className="chart-tip-label">{label}</div>
      {payload.map(p => (
        <div key={p.dataKey} style={{ color: p.color }}>{p.name}: {p.value}</div>
      ))}
    </div>
  );
}

// ─── LIVE CLOCK ───────────────────────────────────────────────────────────────
function LiveClock() {
  const [time, setTime] = useState(() => new Date().toLocaleTimeString('en-GB'));
  useEffect(() => {
    const id = setInterval(() => setTime(new Date().toLocaleTimeString('en-GB')), 1000);
    return () => clearInterval(id);
  }, []);
  return <span className="clock">{time}</span>;
}

// ─── OVERVIEW ─────────────────────────────────────────────────────────────────
function OverviewTab({ pnl, ibkr, sniper, grid, flashArb }) {
  if (!pnl) return <Loader />;

  const todayBNB    = pnl.today?.sniper_bnb   ?? 0;
  const todayUSDT   = pnl.today?.grid_usdt    ?? 0;
  const totalTrades = (pnl.today?.sniper_trades ?? 0) + (pnl.today?.grid_trades ?? 0);
  const winRate     = pnl.today?.sniper_win_rate ?? sniper?.win_rate ?? 0;

  const history   = pnl.history_7d ?? [];
  const chartData = history.map(d => ({
    day:    (d.date ?? '').slice(5),
    sniper: Number(d.sniper_bnb ?? 0),
    grid:   Number(d.grid_usdt  ?? 0),
  }));

  const weekSniper = history.reduce((s, d) => s + (Number(d.sniper_bnb) || 0), 0);
  const weekGrid   = history.reduce((s, d) => s + (Number(d.grid_usdt)  || 0), 0);

  const regime = String(ibkr?.regime?.regime ?? ibkr?.regime ?? 'UNKNOWN');
  const regimeClass = regime.toLowerCase().includes('bull') ? 'bull'
    : regime.toLowerCase().includes('bear') ? 'bear'
    : regime.toLowerCase().includes('range') || regime.toLowerCase().includes('sideways') ? 'range'
    : 'unknown';

  return (
    <div>
      <div className="kpi-grid mb">
        <div className="kpi">
          <div className="kpi-label">// SNIPER P&amp;L TODAY</div>
          <div className={`kpi-val ${todayBNB < 0 ? 'neg' : ''}`}>{fmtBNB(todayBNB)}</div>
          <div className="kpi-sub">BSC NET</div>
        </div>
        <div className="kpi">
          <div className="kpi-label">// GRID P&amp;L TODAY</div>
          <div className={`kpi-val ${todayUSDT < 0 ? 'neg' : ''}`}>{fmtUSD(todayUSDT)}</div>
          <div className="kpi-sub">USDT NET</div>
        </div>
        <div className="kpi">
          <div className="kpi-label">// WIN RATE</div>
          <div className="kpi-val">{pct(winRate)}</div>
          <div className="kpi-sub">SNIPER TODAY</div>
        </div>
        <div className="kpi">
          <div className="kpi-label">// TOTAL TRADES</div>
          <div className="kpi-val neu">{totalTrades}</div>
          <div className="kpi-sub">SNIPER + GRID</div>
        </div>
      </div>

      <div className="row2 mb">
        <div className="panel">
          <div className="panel-head">7-DAY P&amp;L HISTORY</div>
          {chartData.length > 0 ? (
            <ResponsiveContainer width="100%" height={200}>
              <BarChart data={chartData} margin={{ top: 4, right: 4, left: -24, bottom: 0 }}>
                <CartesianGrid strokeDasharray="2 4" stroke="#1a1a1a" vertical={false} />
                <XAxis dataKey="day" tick={{ fill: '#888', fontSize: 10, fontFamily: 'monospace' }} axisLine={false} tickLine={false} />
                <YAxis tick={{ fill: '#888', fontSize: 10, fontFamily: 'monospace' }} axisLine={false} tickLine={false} />
                <Tooltip content={<ChartTip />} />
                <ReferenceLine y={0} stroke="#2a2a2a" />
                <Bar dataKey="sniper" name="Sniper BNB" fill="#00ff88" radius={[2,2,0,0]} />
                <Bar dataKey="grid"   name="Grid USDT"  fill="#00aaff" radius={[2,2,0,0]} />
              </BarChart>
            </ResponsiveContainer>
          ) : (
            <div className="empty">NO DATA</div>
          )}
        </div>

        <div className="panel">
          <div className="panel-head">WEEKLY SUMMARY</div>
          <div className="kv-list">
            <div className="kv">
              <span className="kv-k">REGIME</span>
              <span className={`regime-tag regime-${regimeClass}`}>{regime}</span>
            </div>
            <div className="kv">
              <span className="kv-k">SNIPER 7D</span>
              <span className={clr(weekSniper)}>{fmtBNB(weekSniper)}</span>
            </div>
            <div className="kv">
              <span className="kv-k">GRID 7D</span>
              <span className={clr(weekGrid)}>{fmtUSD(weekGrid)}</span>
            </div>
            {ibkr?.regime?.vix != null && (
              <div className="kv">
                <span className="kv-k">VIX</span>
                <span style={{ color: ibkr.regime.vix > 25 ? 'var(--yellow)' : 'var(--green)' }}>{fmt(ibkr.regime.vix)}</span>
              </div>
            )}
            {ibkr?.regime?.spy != null && (
              <div className="kv">
                <span className="kv-k">SPY</span>
                <span>${fmt(ibkr.regime.spy)}</span>
              </div>
            )}
            {ibkr?.signals?.signals_today != null && (
              <div className="kv">
                <span className="kv-k">IBKR SIGNALS</span>
                <span className="pos">{ibkr.signals.signals_today}</span>
              </div>
            )}
            {grid?.active_bots != null && (
              <div className="kv">
                <span className="kv-k">GRID BOTS ACTIVE</span>
                <span className="pos">{grid.active_bots}</span>
              </div>
            )}
            {flashArb != null && (
              <div className="kv">
                <span className="kv-k">FLASH ARB P&amp;L</span>
                <span className={clr(flashArb.total_pnl_usd ?? 0)}>
                  {fmtUSD(flashArb.total_pnl_usd ?? 0)}
                </span>
              </div>
            )}
            {flashArb?.service_status != null && (
              <div className="kv">
                <span className="kv-k">FLASH ARB BOT</span>
                <span className={`badge ${flashArb.service_status.active ? 'bg' : 'br'}`} style={{ fontSize: 9 }}>
                  {flashArb.service_status.status ?? 'unknown'}
                </span>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

// ─── IBKR ─────────────────────────────────────────────────────────────────────
function IBKRTab({ data }) {
  if (!data) return <Loader />;

  const regime    = String(data.regime?.regime ?? 'UNKNOWN');
  const vix       = data.regime?.vix  ?? null;
  const spy       = data.regime?.spy  ?? null;
  const signals   = data.signals?.recent_signals ?? [];
  const sigCount  = data.signals?.signals_today  ?? signals.length;
  const orders    = data.signals?.orders_placed  ?? 0;

  const regimeClass = regime.toLowerCase().includes('bull') ? 'bull'
    : regime.toLowerCase().includes('bear') ? 'bear'
    : regime.toLowerCase().includes('range') ? 'range'
    : 'unknown';

  return (
    <div>
      <div className="kpi-grid mb">
        <div className="kpi">
          <div className="kpi-label">// REGIME</div>
          <div style={{ marginTop: 8 }}>
            <span className={`regime-tag regime-${regimeClass}`}>{regime}</span>
          </div>
        </div>
        <div className="kpi">
          <div className="kpi-label">// VIX</div>
          <div className="kpi-val" style={{ color: vix != null && vix > 25 ? 'var(--yellow)' : 'var(--green)' }}>
            {vix != null ? fmt(vix) : '—'}
          </div>
        </div>
        <div className="kpi">
          <div className="kpi-label">// SPY</div>
          <div className="kpi-val neu">{spy != null ? `$${fmt(spy)}` : '—'}</div>
        </div>
        <div className="kpi">
          <div className="kpi-label">// SIGNALS TODAY</div>
          <div className="kpi-val neu">{sigCount}</div>
          <div className="kpi-sub">{orders} ORDERS PLACED</div>
        </div>
      </div>

      <div className="panel">
        <div className="panel-head">RECENT SIGNALS — {data.account} ({data.mode})</div>
        {signals.length === 0 ? (
          <div className="empty">NO RECENT SIGNALS</div>
        ) : (
          <div className="tbl-wrap">
            <table>
              <thead>
                <tr>
                  <th>Ticker</th>
                  <th>Dir</th>
                  <th>Price</th>
                  <th>Stop</th>
                  <th>TP</th>
                  <th>Score</th>
                  <th>Strategy</th>
                  <th>Time</th>
                </tr>
              </thead>
              <tbody>
                {signals.map((s, i) => (
                  <tr key={i}>
                    <td style={{ color: 'var(--green)', fontWeight: 700 }}>{s.ticker ?? '—'}</td>
                    <td>
                      <span className={`badge ${(s.direction ?? '').toLowerCase() === 'long' ? 'bg' : 'br'}`}>
                        {s.direction ?? '—'}
                      </span>
                    </td>
                    <td>{s.price != null ? `$${fmt(s.price)}` : '—'}</td>
                    <td className="neg">{s.stop_loss   != null ? `$${fmt(s.stop_loss)}` : '—'}</td>
                    <td className="pos">{s.take_profit != null ? `$${fmt(s.take_profit)}` : '—'}</td>
                    <td style={{ color: 'var(--blue)' }}>{s.score ?? '—'}</td>
                    <td className="dim">{s.strategy ?? '—'}</td>
                    <td className="dim" style={{ fontSize: 10 }}>{s.timestamp ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

// ─── SNIPER ───────────────────────────────────────────────────────────────────
function SniperTab({ data }) {
  if (!data) return <Loader />;

  const trades   = data.recent_trades  ?? [];
  const history  = data.pnl_history    ?? [];
  const totalBNB = data.total_pnl_bnb  ?? 0;
  const totalUSD = data.total_pnl_usdt ?? 0;
  const winRate  = data.win_rate       ?? 0;
  const wins     = data.wins           ?? 0;
  const losses   = data.losses         ?? 0;

  const chartData = history.map(d => ({
    day:  (d.date ?? '').slice(5),
    bnb:  Number(d.pnl_bnb  ?? 0),
    usdt: Number(d.pnl_usdt ?? 0),
  }));

  return (
    <div>
      <div className="kpi-grid mb">
        <div className="kpi">
          <div className="kpi-label">// WIN RATE</div>
          <div className="kpi-val">{pct(winRate)}</div>
          <div className="kpi-sub">{wins}W / {losses}L</div>
        </div>
        <div className="kpi">
          <div className="kpi-label">// P&amp;L BNB (7D)</div>
          <div className={`kpi-val ${totalBNB < 0 ? 'neg' : ''}`} style={{ fontSize: 18 }}>{fmtBNB(totalBNB)}</div>
        </div>
        <div className="kpi">
          <div className="kpi-label">// P&amp;L USDT (7D)</div>
          <div className={`kpi-val ${totalUSD < 0 ? 'neg' : ''}`} style={{ fontSize: 18 }}>{fmtUSD(totalUSD)}</div>
        </div>
        <div className="kpi">
          <div className="kpi-label">// TOTAL TRADES</div>
          <div className="kpi-val neu">{data.total_trades ?? 0}</div>
        </div>
      </div>

      {chartData.length > 0 && (
        <div className="panel mb">
          <div className="panel-head">P&amp;L 7 DAYS</div>
          <ResponsiveContainer width="100%" height={160}>
            <BarChart data={chartData} margin={{ top: 4, right: 4, left: -24, bottom: 0 }}>
              <CartesianGrid strokeDasharray="2 4" stroke="#1a1a1a" vertical={false} />
              <XAxis dataKey="day" tick={{ fill: '#888', fontSize: 10, fontFamily: 'monospace' }} axisLine={false} tickLine={false} />
              <YAxis tick={{ fill: '#888', fontSize: 10, fontFamily: 'monospace' }} axisLine={false} tickLine={false} />
              <Tooltip content={<ChartTip />} />
              <ReferenceLine y={0} stroke="#2a2a2a" />
              <Bar dataKey="bnb"  name="BNB"  fill="#00ff88" radius={[2,2,0,0]} />
              <Bar dataKey="usdt" name="USDT" fill="#00aaff" radius={[2,2,0,0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}

      <div className="panel">
        <div className="panel-head">RECENT TRADES — BSC SNIPER</div>
        {trades.length === 0 ? (
          <div className="empty">NO RECENT TRADES</div>
        ) : (
          <div className="tbl-wrap">
            <table>
              <thead>
                <tr>
                  <th>Token</th>
                  <th>Result</th>
                  <th>P&amp;L</th>
                  <th>Currency</th>
                  <th>Time</th>
                </tr>
              </thead>
              <tbody>
                {[...trades].reverse().map((t, i) => (
                  <tr key={i}>
                    <td style={{ color: 'var(--text-sec)', fontWeight: 700, fontSize: 11 }}>{t.token ?? '—'}</td>
                    <td>
                      <span className={`badge ${t.type === 'TAKE_PROFIT' ? 'bg' : 'br'}`}>
                        {t.type === 'TAKE_PROFIT' ? 'TP' : t.type === 'STOP_LOSS' ? 'SL' : t.type ?? '—'}
                      </span>
                    </td>
                    <td className={clr(t.pnl)}>
                      {t.pnl != null ? (t.pnl >= 0 ? '+' : '') + Number(t.pnl).toFixed(6) : '—'}
                    </td>
                    <td>
                      <span className={`badge ${t.currency === 'WBNB' ? 'by' : 'bb'}`}>{t.currency ?? '—'}</span>
                    </td>
                    <td className="dim" style={{ fontSize: 10 }}>{t.timestamp ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

// ─── GRID ─────────────────────────────────────────────────────────────────────
function GridTab({ data }) {
  if (!data) return <Loader />;

  const bots   = data.active_grids  ?? [];
  const trades = data.recent_trades ?? [];

  return (
    <div>
      <div className="row3 mb">
        <div className="kpi">
          <div className="kpi-label">// ACTIVE BOTS</div>
          <div className="kpi-val neu">{data.active_bots ?? bots.length}</div>
        </div>
        <div className="kpi">
          <div className="kpi-label">// TOTAL P&amp;L</div>
          <div className={`kpi-val ${(data.total_pnl ?? 0) < 0 ? 'neg' : ''}`}>{fmtUSD(data.total_pnl)}</div>
        </div>
        <div className="kpi">
          <div className="kpi-label">// GRID TRADES</div>
          <div className="kpi-val neu">{data.total_trades ?? trades.length}</div>
        </div>
      </div>

      <div className="panel mb">
        <div className="panel-head">ACTIVE GRID BOTS</div>
        {bots.length === 0 ? (
          <div className="empty">NO ACTIVE BOTS</div>
        ) : (
          <div className="tbl-wrap">
            <table>
              <thead>
                <tr>
                  <th>Bot</th>
                  <th>Pair</th>
                  <th>Price</th>
                  <th>Lower</th>
                  <th>Upper</th>
                  <th>Range</th>
                  <th>Levels</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {bots.map((b, i) => (
                  <tr key={i}>
                    <td className="dim" style={{ fontSize: 10 }}>{b.bot ?? '—'}</td>
                    <td style={{ color: 'var(--green)', fontWeight: 700 }}>{b.pair ?? '—'}</td>
                    <td>{b.price != null ? fmt(b.price, 4) : '—'}</td>
                    <td className="dim">{b.lower != null ? fmt(b.lower, 4) : '—'}</td>
                    <td className="dim">{b.upper != null ? fmt(b.upper, 4) : '—'}</td>
                    <td>{b.range ?? '—'}</td>
                    <td>{b.levels ?? '—'}</td>
                    <td><span className="badge by">dry_run</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {trades.length > 0 && (
        <div className="panel">
          <div className="panel-head">RECENT TRADES</div>
          <div className="tbl-wrap">
            <table>
              <thead>
                <tr>
                  <th>Pair</th>
                  <th>Side</th>
                  <th>Price</th>
                  <th>P&amp;L</th>
                  <th>Time</th>
                </tr>
              </thead>
              <tbody>
                {trades.slice(0, 30).map((t, i) => (
                  <tr key={i}>
                    <td style={{ fontWeight: 700 }}>{t.pair ?? '—'}</td>
                    <td>
                      <span className={`badge ${(t.side ?? '').toLowerCase() === 'buy' ? 'bg' : 'br'}`}>
                        {t.side ?? '—'}
                      </span>
                    </td>
                    <td>{t.price != null ? fmt(t.price, 4) : '—'}</td>
                    <td className={clr(t.pnl)}>{t.pnl != null ? fmtUSD(t.pnl) : 'dry_run'}</td>
                    <td className="dim" style={{ fontSize: 10 }}>{t.timestamp ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

// ─── FUNDING ──────────────────────────────────────────────────────────────────
function FundingTab({ data }) {
  if (!data) return <Loader />;

  const positions   = data.positions         ?? [];
  const totalEarned = data.total_earned_usdt ?? null;

  return (
    <div>
      <div className="row2 mb">
        <div className="kpi">
          <div className="kpi-label">// OPEN POSITIONS</div>
          <div className="kpi-val neu">{data.active_positions ?? positions.length}</div>
        </div>
        <div className="kpi">
          <div className="kpi-label">// FUNDING EARNED</div>
          <div className={`kpi-val ${(totalEarned ?? 0) < 0 ? 'neg' : ''}`}>{fmtUSD(totalEarned)}</div>
        </div>
      </div>

      <div className="panel">
        <div className="panel-head">FUNDING RATE POSITIONS</div>
        {positions.length === 0 ? (
          <div className="empty">NO OPEN POSITIONS</div>
        ) : (
          <div className="tbl-wrap">
            <table>
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th>Side</th>
                  <th>Size</th>
                  <th>Entry</th>
                  <th>Mark</th>
                  <th>Funding Earned</th>
                  <th>Unr. P&amp;L</th>
                </tr>
              </thead>
              <tbody>
                {positions.map((p, i) => (
                  <tr key={i}>
                    <td style={{ color: 'var(--green)', fontWeight: 700 }}>{p.symbol ?? '—'}</td>
                    <td>
                      <span className={`badge ${(p.side ?? '').toLowerCase() === 'long' ? 'bg' : 'br'}`}>
                        {p.side ?? '—'}
                      </span>
                    </td>
                    <td>{p.size  != null ? fmtUSD(p.size)  : '—'}</td>
                    <td className="dim">{p.entry != null ? fmt(p.entry, 4) : '—'}</td>
                    <td className="dim">{p.mark  != null ? fmt(p.mark,  4) : '—'}</td>
                    <td className={clr(p.funding_earned)}>{fmtUSD(p.funding_earned)}</td>
                    <td className={clr(p.unrealized_pnl)}>{fmtUSD(p.unrealized_pnl)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

// ─── FLASH ARB ────────────────────────────────────────────────────────────────
function FlashArbTab({ data }) {
  if (!data) return <Loader />;

  const svc      = data.service_status         ?? {};
  const isActive = svc.active                  ?? false;
  const trades   = data.recent_trades          ?? [];
  const opps     = data.recent_opps            ?? [];
  const logs     = data.recent_logs            ?? [];
  const totalPnl = data.total_pnl_usd          ?? 0;
  const executed = data.trades_executed        ?? 0;
  const detected = data.opportunities_detected ?? 0;
  const lastSpread = data.last_spread          ?? null;
  const lastPair   = data.last_spread_pair     ?? '—';
  const ethBal     = data.eth_balance          ?? null;

  function parseLevel(line) {
    if (/ERROR|CRITICAL/i.test(line)) return 'ERROR';
    if (/WARN/i.test(line))           return 'WARN';
    return 'INFO';
  }

  return (
    <div>
      <div className="kpi-grid mb">
        <div className="kpi">
          <div className="kpi-label">// SERVIÇO</div>
          <div style={{ marginTop: 8 }}>
            <span className={`badge ${isActive ? 'bg' : 'br'}`}>{svc.status ?? 'unknown'}</span>
          </div>
          <div className="kpi-sub">{ethBal != null ? `${Number(ethBal).toFixed(6)} ETH` : '—'}</div>
        </div>
        <div className="kpi">
          <div className="kpi-label">// P&amp;L ACUMULADO</div>
          <div className={`kpi-val ${totalPnl < 0 ? 'neg' : ''}`}>{fmtUSD(totalPnl)}</div>
          <div className="kpi-sub">{executed} TRADES EXEC.</div>
        </div>
        <div className="kpi">
          <div className="kpi-label">// OPORTUNIDADES</div>
          <div className="kpi-val neu">{detected}</div>
          <div className="kpi-sub">{executed} EXECUTADAS</div>
        </div>
        <div className="kpi">
          <div className="kpi-label">// ÚLTIMO SPREAD</div>
          <div className="kpi-val" style={{ fontSize: 18 }}>
            {lastSpread != null ? `${Number(lastSpread).toFixed(4)}%` : '—'}
          </div>
          <div className="kpi-sub">{lastPair}</div>
        </div>
      </div>

      <div className="panel mb">
        <div className="panel-head">TRADES EXECUTADOS — FLASH ARB BASE</div>
        {trades.length === 0 ? (
          <div className="empty">SEM TRADES — A MONITORIZAR SPREADS…</div>
        ) : (
          <div className="tbl-wrap">
            <table>
              <thead>
                <tr>
                  <th>Par</th>
                  <th>Spread</th>
                  <th>Profit</th>
                  <th>P&amp;L Total</th>
                  <th>Tx</th>
                  <th>Hora</th>
                </tr>
              </thead>
              <tbody>
                {[...trades].reverse().map((t, i) => (
                  <tr key={i}>
                    <td style={{ color: 'var(--green)', fontWeight: 700 }}>{t.pair ?? '—'}</td>
                    <td style={{ color: 'var(--blue)' }}>{t.spread != null ? `${Number(t.spread).toFixed(4)}%` : '—'}</td>
                    <td className={clr(t.profit)}>{t.profit != null ? fmtUSD(t.profit) : '—'}</td>
                    <td className="pos">{t.total != null ? fmtUSD(t.total) : '—'}</td>
                    <td className="dim" style={{ fontSize: 10, fontFamily: 'monospace' }}>
                      {t.tx ? t.tx.slice(0, 14) + '…' : '—'}
                    </td>
                    <td className="dim" style={{ fontSize: 10 }}>{t.timestamp ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {opps.length > 0 && (
        <div className="panel mb">
          <div className="panel-head">OPORTUNIDADES RECENTES</div>
          <div className="tbl-wrap">
            <table>
              <thead>
                <tr>
                  <th>Par</th>
                  <th>Spread</th>
                  <th>Direção</th>
                  <th>Hora</th>
                </tr>
              </thead>
              <tbody>
                {[...opps].reverse().map((o, i) => (
                  <tr key={i}>
                    <td style={{ color: 'var(--text-sec)', fontWeight: 700 }}>{o.pair ?? '—'}</td>
                    <td style={{ color: 'var(--yellow)' }}>{o.spread != null ? `${Number(o.spread).toFixed(4)}%` : '—'}</td>
                    <td><span className="badge by">{o.reverse ? 'AERO→UNI' : 'UNI→AERO'}</span></td>
                    <td className="dim" style={{ fontSize: 10 }}>{o.timestamp ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <div className="panel">
        <div className="panel-head">LOGS EM TEMPO REAL</div>
        <div className="log-console" style={{ maxHeight: 280 }}>
          {logs.length === 0 && <span className="dim">~ sem logs ~</span>}
          {logs.slice(-60).map((line, i) => {
            const level   = parseLevel(line);
            const tsMatch = line.match(/^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})/);
            const ts      = tsMatch ? tsMatch[1] : null;
            const rest    = ts ? line.slice(ts.length).trim() : line;
            return (
              <span key={i} className="log-line">
                {ts && <span className="log-ts">{ts}</span>}
                <span className={`log-${level}`}>[{level}]</span>{' '}
                <span className="log-text">{rest}</span>{'\n'}
              </span>
            );
          })}
        </div>
      </div>
    </div>
  );
}

// ─── LOGS ─────────────────────────────────────────────────────────────────────
const LOG_SERVICES = [
  { label: 'autonomous-trader', value: 'autonomous-trader' },
  { label: 'crypto_bsc',        value: 'crypto_bsc' },
  { label: 'ibc-gateway',       value: 'ibc-gateway' },
  { label: 'tgbot-ibkr',        value: 'tgbot-ibkr' },
  { label: 'tgbot-sniper',      value: 'tgbot-sniper' },
  { label: 'tgbot-grid',        value: 'tgbot-grid' },
  { label: 'tgbot-funding',     value: 'tgbot-funding' },
  { label: 'hermes-gateway',    value: 'hermes-gateway' },
  { label: 'flash-arb-bot',     value: 'flash-arb-bot'  },
];

// ─── LIQUIDATIONS TAB ─────────────────────────────────────────────────────────
function LiquidationsPanel({ data }) {
  if (!data) return <Loader />;
  const opps    = data.opportunities ?? [];
  const summary = data.summary       ?? {};
  const totalEstProfit = Number(summary.total_est_profit) || 0;

  const [minProfit,  setMinProfit]  = useState('25');
  const [dateFilter, setDateFilter] = useState('hoje');

  const _now   = new Date();
  const _today = new Date(_now.getFullYear(), _now.getMonth(), _now.getDate());
  const _dateThreshold = {
    hoje:   _today,
    ontem:  new Date(_today - 86400000),
    '7dias': new Date(_today - 6 * 86400000),
    tudo:   null,
  }[dateFilter];

  const inRange = (o) => {
    if (!_dateThreshold) return true;
    const ts = new Date(o.ts);
    if (dateFilter === 'ontem') return ts >= new Date(_today - 86400000) && ts < _today;
    return ts >= _dateThreshold;
  };

  const filteredOpps = opps.filter(inRange);
  const liquidable = filteredOpps.filter(o => o.health_factor < 1.0);
  const watching   = filteredOpps.filter(o => o.health_factor >= 1.0 && o.health_factor < 1.2)
                        .filter(o => (Number(o.estimated_profit) || 0) >= (Number(minProfit) || 0))
                        .sort((a, b) => a.health_factor - b.health_factor);

  const thead = (
    <thead>
      <tr>
        <th>Posição</th>
        <th>HF</th>
        <th>Dívida USD</th>
        <th>Lucro Est.</th>
      </tr>
    </thead>
  );

  const renderRow = (o, i, isLiquidable) => (
    <tr key={i}>
      <td title={o.position_address}>
        {o.position_address ? o.position_address.slice(0, 10) + '…' : '—'}
      </td>
      <td className={o.health_factor < 1.0 ? 'neg' : 'pos'}>
        {fmt(o.health_factor, 4)}
      </td>
      <td>{fmtUSD(o.debt_usd)}</td>
      <td className={isLiquidable ? 'pos' : ''} style={isLiquidable ? {} : { color: '#4a7a4a' }}>
        {fmtUSD(o.estimated_profit)}
      </td>
    </tr>
  );

  return (
    <div>
      <div className="kpi-row">
        <div className="kpi-card">
          <div className="kpi-label">Oportunidades</div>
          <div className="kpi-value">{summary.total ?? 0}</div>
        </div>
        <div className="kpi-card">
          <div className="kpi-label">Executadas</div>
          <div className="kpi-value">{summary.executed ?? 0}</div>
        </div>
        <div className="kpi-card">
          <div className="kpi-label">Lucro Estimado</div>
          <div className={`kpi-value ${clr(totalEstProfit)}`}>
            {fmtUSD(totalEstProfit)}
          </div>
        </div>
        <div className="kpi-card">
          <div className="kpi-label">Melhor Oportunidade</div>
          <div className={`kpi-value ${clr(summary.best_profit)}`}>
            {fmtUSD(summary.best_profit)}
          </div>
        </div>
      </div>

      <div style={{ display: 'flex', gap: '0.5rem', marginBottom: '1rem' }}>
        {[['hoje','Hoje'],['ontem','Ontem'],['7dias','7 dias'],['tudo','Tudo']].map(([val, label]) => (
          <button key={val} onClick={() => setDateFilter(val)} style={{
            padding: '0.3rem 0.8rem', borderRadius: 4, border: 'none', cursor: 'pointer',
            fontSize: '0.85em', fontWeight: dateFilter === val ? 700 : 400,
            background: dateFilter === val ? '#e8b800' : '#2a2a2a',
            color: dateFilter === val ? '#111' : '#ccc',
          }}>{label}</button>
        ))}
      </div>

      <div style={{ marginBottom: '1rem', background: '#1a0000', borderRadius: 6, padding: '0.75rem 1rem' }}>
        <div style={{ fontWeight: 700, marginBottom: '0.5rem' }}>🔴 Liquidáveis Agora ({liquidable.length})</div>
        {liquidable.length === 0 ? (
          <div style={{ color: '#888', fontSize: '0.9em' }}>Sem posições liquidáveis agora</div>
        ) : (
          <div style={{ overflowX: 'auto' }}><table>{thead}<tbody>{liquidable.map((o, i) => renderRow(o, i, true))}</tbody></table></div>
        )}
      </div>

      <div style={{ background: '#1a1500', borderRadius: 6, padding: '0.75rem 1rem' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '1rem', marginBottom: '0.5rem' }}>
          <span style={{ fontWeight: 700 }}>🟡 Em Vigilância ({watching.length})</span>
          <label style={{ display: 'flex', alignItems: 'center', gap: '0.4rem', fontSize: '0.85em', color: '#ccc' }}>
            Lucro mín. $
            <input
              type="number"
              min="0"
              step="5"
              value={minProfit}
              onChange={e => setMinProfit(e.target.value)}
              onFocus={() => setMinProfit('')}
              onBlur={e => { if (e.target.value.trim() === '') setMinProfit('25'); }}
              style={{ width: '5rem', padding: '0.2rem 0.4rem', borderRadius: 4, border: '1px solid #555', background: '#111', color: '#fff', fontSize: '0.9em' }}
            />
          </label>
        </div>
        {watching.length === 0 ? (
          <div style={{ color: '#888', fontSize: '0.9em' }}>Sem posições em vigilância com lucro ≥ ${minProfit}</div>
        ) : (
          <div style={{ overflowX: 'auto' }}><table>{thead}<tbody>{watching.map((o, i) => renderRow(o, i, false))}</tbody></table></div>
        )}
      </div>
    </div>
  );
}

function LiquidationsTab({ dataBase, dataPolygon, dataAvax, dataArb }) {
  const [subTab, setSubTab] = useState('base');

  const CHAINS = [
    { id: 'base',    label: 'Base',      activeColor: '#2d6ae0' },
    { id: 'polygon', label: 'Polygon',   activeColor: '#8247e5' },
    { id: 'avax',    label: 'Avalanche', activeColor: '#e84142' },
    { id: 'arb',     label: 'Arbitrum',  activeColor: '#28A0F0' },
  ];

  const subBtnStyle = (id, activeColor) => ({
    padding: '0.4rem 1.3rem',
    borderRadius: 4,
    border: subTab === id ? `2px solid ${activeColor}` : '2px solid transparent',
    cursor: 'pointer',
    fontSize: '0.9em',
    fontWeight: subTab === id ? 700 : 400,
    background: subTab === id ? activeColor : '#2a2a2a',
    color: '#fff',
    transition: 'background 0.15s',
  });

  const dataMap = { base: dataBase, polygon: dataPolygon, avax: dataAvax, arb: dataArb };
  const active = dataMap[subTab];
  const chainLabel = CHAINS.find(c => c.id === subTab)?.label ?? subTab;

  return (
    <div>
      <div style={{ display: 'flex', gap: '0.5rem', marginBottom: '1.25rem', borderBottom: '1px solid #333', paddingBottom: '0.75rem' }}>
        {CHAINS.map(({ id, label, activeColor }) => (
          <button key={id} onClick={() => setSubTab(id)} style={subBtnStyle(id, activeColor)}>
            {label}
          </button>
        ))}
      </div>
      {!active
        ? <div style={{ color: '#888', padding: '2rem 0', textAlign: 'center' }}>
            Sem dados {chainLabel} ainda
          </div>
        : <LiquidationsPanel key={subTab} data={active} />
      }
    </div>
  );
}

function LogsTab() {
  const [service, setService] = useState('autonomous-trader');
  const [lines,   setLines]   = useState([]);
  const [loading, setLoading] = useState(false);
  const [error,   setError]   = useState(null);
  const bottomRef = useRef(null);

  const fetchLogs = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await apiFetch(`/api/logs/${service}`);
      const raw = (data.logs ?? []).map(l => (typeof l === 'object' ? l.raw : l) ?? '');
      setLines(raw);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [service]);

  useEffect(() => { fetchLogs(); }, [fetchLogs]);
  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: 'smooth' }); }, [lines]);

  function parseLevel(line) {
    if (/ERROR|CRITICAL|FATAL/i.test(line)) return 'ERROR';
    if (/WARN/i.test(line))  return 'WARN';
    if (/DEBUG/i.test(line)) return 'DEBUG';
    return 'INFO';
  }

  return (
    <div className="panel">
      <div className="log-bar">
        <div className="panel-head" style={{ margin: 0, padding: 0, border: 'none' }}>LOG VIEWER</div>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <select className="sel" value={service} onChange={e => setService(e.target.value)}>
            {LOG_SERVICES.map(s => <option key={s.value} value={s.value}>{s.label}</option>)}
          </select>
          <button className={`btn ${loading ? 'spin' : ''}`} onClick={fetchLogs}>
            <RefreshCw size={11} /> refresh
          </button>
        </div>
      </div>

      {error ? <Err msg={error} /> : (
        <div className="log-console">
          {lines.length === 0 && !loading && <span className="dim">~ no logs ~</span>}
          {lines.map((line, i) => {
            const level = parseLevel(line);
            const tsMatch = line.match(/^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[^\s]*)/);
            const ts   = tsMatch ? tsMatch[1] : null;
            const rest = ts ? line.slice(ts.length).trim() : line;
            return (
              <span key={i} className="log-line">
                {ts && <span className="log-ts">{ts}</span>}
                <span className={`log-${level}`}>[{level}]</span>{' '}
                <span className="log-text">{rest}</span>{'\n'}
              </span>
            );
          })}
          <div ref={bottomRef} />
        </div>
      )}
    </div>
  );
}

// ─── SYSTEM ───────────────────────────────────────────────────────────────────
function SystemTab({ data }) {
  if (!data) return <Loader />;

  const cpu  = data.cpu_percent     ?? null;
  const ram  = data.memory_used_pct ?? null;
  const disk = typeof data.disk_used_pct === 'string'
    ? parseFloat(data.disk_used_pct)
    : data.disk_used_pct ?? null;

  function gaugeColor(v) {
    if (v >= 90) return 'var(--red)';
    if (v >= 70) return 'var(--yellow)';
    return 'var(--green)';
  }
  function gaugeClass(v) {
    if (v >= 90) return 'danger';
    if (v >= 70) return 'warn';
    return '';
  }

  const gauges = [
    { label: 'CPU',  icon: <Cpu size={12} />,         value: cpu,  extra: null },
    { label: 'RAM',  icon: <MemoryStick size={12} />,  value: ram,  extra: null },
    { label: 'DISK', icon: <HardDrive size={12} />,    value: disk, extra: `${data.disk_used ?? '?'} / ${data.disk_size ?? '?'}` },
  ].filter(g => g.value != null);

  const serviceList = Array.isArray(data.services)
    ? data.services
    : Object.entries(data.services ?? {}).map(([k, v]) => ({ service: k, status: v, active: v === 'active' || v === true }));

  return (
    <div>
      <div className="row3 mb">
        {gauges.map(g => (
          <div key={g.label} className="panel">
            <div className="panel-head">{g.icon}&nbsp;{g.label}</div>
            <div className="gauge-val" style={{ color: gaugeColor(g.value) }}>
              {fmt(g.value, 1)}%
            </div>
            {g.extra && <div className="dim" style={{ fontSize: 10, marginBottom: 8 }}>{g.extra}</div>}
            <div className="bar-wrap">
              <div className={`bar-fill ${gaugeClass(g.value)}`} style={{ width: `${Math.min(g.value, 100)}%` }} />
            </div>
          </div>
        ))}
      </div>

      {data.load_avg && (
        <div className="panel mb">
          <div className="panel-head">LOAD AVERAGE</div>
          <div style={{ display: 'flex', gap: 32 }}>
            {(Array.isArray(data.load_avg) ? data.load_avg : [data.load_avg]).map((v, i) => (
              <div key={i}>
                <div className="dim" style={{ fontSize: 10, marginBottom: 4 }}>{['1m','5m','15m'][i] ?? `${i}`}</div>
                <div style={{ fontSize: 22, fontWeight: 700, color: 'var(--green)' }}>{fmt(Number(v), 2)}</div>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="panel">
        <div className="panel-head">SERVICES</div>
        {serviceList.length === 0 ? (
          <div className="empty">NO SERVICE DATA</div>
        ) : (
          serviceList.map(svc => {
            const name   = svc.service ?? svc.name ?? '?';
            const isUp   = svc.active ?? (svc.status === 'active' || svc.status === 'running');
            const status = svc.status ?? (isUp ? 'active' : 'down');
            const since  = svc.uptime_since ?? '';
            return (
              <div key={name} className="svc-row">
                <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                  {isUp
                    ? <CheckCircle size={13} style={{ color: 'var(--green)', flexShrink: 0 }} />
                    : <XCircle    size={13} style={{ color: status === 'failed' ? 'var(--red)' : 'var(--yellow)', flexShrink: 0 }} />}
                  <div>
                    <div className="svc-name">{name}</div>
                    {since && <div className="svc-since">{since}</div>}
                  </div>
                </div>
                <span className={`badge ${isUp ? 'bg' : status === 'failed' ? 'br' : 'by'}`}>{status}</span>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}

// ─── REPORT MODAL ─────────────────────────────────────────────────────────────
function ReportModal({ html, mode, onClose }) {
  useEffect(() => {
    const prev = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    const onKey = e => { if (e.key === 'Escape') onClose(); };
    document.addEventListener('keydown', onKey);
    return () => {
      document.body.style.overflow = prev;
      document.removeEventListener('keydown', onKey);
    };
  }, [onClose]);

  function download() {
    const blob = new Blob([html], { type: 'text/html' });
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement('a');
    a.href     = url;
    a.download = `trader-report-${new Date().toISOString().slice(0, 10)}.html`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }

  return (
    <div className="report-overlay" onClick={onClose}>
      <div className="report-modal" onClick={e => e.stopPropagation()}>
        <div className="report-modal-head">
          <div className="report-modal-title">
            REPORT
            <span className={`badge ${mode === 'ai' ? 'badge-ai' : 'badge-auto'}`}>
              {mode === 'ai' ? 'AI Report' : 'Auto Report'}
            </span>
          </div>
          <div className="report-modal-actions">
            <button className="btn" onClick={download} title="Download HTML">
              <Download size={11} /> download
            </button>
            <button className="btn btn-close" onClick={onClose} title="Fechar (Esc)">
              <X size={13} />
            </button>
          </div>
        </div>
        <div className="report-modal-body">
          <iframe
            srcDoc={html}
            title="Trader Report"
            className="report-iframe"
          />
        </div>
      </div>
    </div>
  );
}

// ─── ROOT APP ─────────────────────────────────────────────────────────────────
const TABS = [
  { id: 'overview',   label: 'Overview',   icon: BarChart2  },
  { id: 'ibkr',       label: 'IBKR',       icon: TrendingUp },
  { id: 'sniper',     label: 'Sniper',     icon: Target     },
  { id: 'grid',       label: 'Grid',       icon: Grid       },
  { id: 'funding',    label: 'Funding',    icon: DollarSign },
  { id: 'flash-arb',  label: 'Flash Arb',  icon: Zap        },
  { id: 'liquidations', label: 'Liquidations', icon: Activity },
  { id: 'logs',       label: 'Logs',       icon: FileText   },
  { id: 'system',     label: 'System',     icon: Server     },
];

export default function App() {
  const [activeTab,     setActiveTab]     = useState('overview');
  const [loading,       setLoading]       = useState(false);
  const [online,        setOnline]        = useState(null);
  const [errors,        setErrors]        = useState({});
  const [reportLoading, setReportLoading] = useState(false);
  const [report,        setReport]        = useState(null);

  const [pnl,      setPnl]      = useState(null);
  const [ibkr,     setIbkr]     = useState(null);
  const [sniper,   setSniper]   = useState(null);
  const [grid,     setGrid]     = useState(null);
  const [funding,  setFunding]  = useState(null);
  const [system,   setSystem]   = useState(null);
  const [flashArb,            setFlashArb]            = useState(null);
  const [liquidations,        setLiquidations]        = useState(null);
  const [liquidationsPolygon, setLiquidationsPolygon] = useState(null);
  const [liquidationsAvax,    setLiquidationsAvax]    = useState(null);
  const [liquidationsArb,     setLiquidationsArb]     = useState(null);

  const fetchAll = useCallback(async () => {
    setLoading(true);
    const errs = {};
    const [pnlR, ibkrR, sniperR, gridR, fundingR, systemR, flashArbR, liquidationsR, liquidationsPolygonR, liquidationsAvaxR, liquidationsArbR] = await Promise.allSettled([
      apiFetch('/api/pnl'),
      apiFetch('/api/ibkr'),
      apiFetch('/api/sniper'),
      apiFetch('/api/grid'),
      apiFetch('/api/funding'),
      apiFetch('/api/system'),
      apiFetch('/api/flash-arb'),
      apiFetch('/api/liquidations'),
      apiFetch('/api/liquidations/polygon'),
      apiFetch('/api/liquidations/avax'),
      apiFetch('/api/liquidations/arb'),
    ]);
    if (pnlR.status                === 'fulfilled') setPnl(pnlR.value);                           else errs.pnl          = pnlR.reason?.message;
    if (ibkrR.status               === 'fulfilled') setIbkr(ibkrR.value);                         else errs.ibkr         = ibkrR.reason?.message;
    if (sniperR.status             === 'fulfilled') setSniper(sniperR.value);                     else errs.sniper       = sniperR.reason?.message;
    if (gridR.status               === 'fulfilled') setGrid(gridR.value);                         else errs.grid         = gridR.reason?.message;
    if (fundingR.status            === 'fulfilled') setFunding(fundingR.value);                   else errs.funding      = fundingR.reason?.message;
    if (systemR.status             === 'fulfilled') setSystem(systemR.value);                     else errs.system       = systemR.reason?.message;
    if (flashArbR.status           === 'fulfilled') setFlashArb(flashArbR.value);                 else errs.flashArb     = flashArbR.reason?.message;
    if (liquidationsR.status       === 'fulfilled') setLiquidations(liquidationsR.value);         else errs.liquidations = liquidationsR.reason?.message;
    if (liquidationsPolygonR.status === 'fulfilled') setLiquidationsPolygon(liquidationsPolygonR.value);
    if (liquidationsAvaxR.status    === 'fulfilled') setLiquidationsAvax(liquidationsAvaxR.value);
    if (liquidationsArbR.status     === 'fulfilled') setLiquidationsArb(liquidationsArbR.value);
    setErrors(errs);
    setOnline(Object.keys(errs).length < 7);
    setLoading(false);
  }, []);

  useEffect(() => {
    fetchAll();
    const id = setInterval(fetchAll, 30_000);
    return () => clearInterval(id);
  }, [fetchAll]);

  const generateReport = useCallback(async () => {
    setReportLoading(true);
    try {
      const res = await fetch('/api/report');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setReport(data);
    } catch (e) {
      console.error('Report error:', e);
    } finally {
      setReportLoading(false);
    }
  }, []);

  return (
    <div className="app">
      {report && (
        <ReportModal
          html={report.html}
          mode={report.mode}
          onClose={() => setReport(null)}
        />
      )}
      <header className="header">
        <div className="header-brand">
          <div className="header-title">TRADER DASHBOARD</div>
          <div className="header-sub">Algorithmic Trading Monitor</div>
        </div>
        <div className="header-right">
          <LiveClock />
          <div className="status-pill">
            <div className={`dot ${online === false ? 'offline' : ''}`} />
            {online === null ? 'connecting...' : online ? 'online' : 'partial'}
          </div>
          <button
            className={`btn btn-report${reportLoading ? ' spin' : ''}`}
            onClick={generateReport}
            disabled={reportLoading}
            title="Gerar relatório de análise"
          >
            {reportLoading
              ? <><RefreshCw size={11} /> generating...</>
              : <><FileText size={11} /> report</>}
          </button>
          <button className={`btn ${loading ? 'spin' : ''}`} onClick={fetchAll}>
            <RefreshCw size={11} /> refresh
          </button>
        </div>
      </header>

      <nav className="tabs">
        {TABS.map(t => {
          const Icon   = t.icon;
          const hasErr = errors[t.id];
          return (
            <button
              key={t.id}
              className={`tab ${activeTab === t.id ? 'active' : ''}`}
              onClick={() => setActiveTab(t.id)}
            >
              <Icon size={13} />
              {t.label}
              {hasErr && <span className="tab-err" />}
            </button>
          );
        })}
      </nav>

      <main className="content" key={activeTab}>
        {activeTab === 'overview'  && <OverviewTab pnl={pnl} ibkr={ibkr} sniper={sniper} grid={grid} flashArb={flashArb} />}
        {activeTab === 'ibkr'     && (errors.ibkr    ? <Err msg={errors.ibkr}    /> : <IBKRTab    data={ibkr}    />)}
        {activeTab === 'sniper'   && (errors.sniper  ? <Err msg={errors.sniper}  /> : <SniperTab  data={sniper}  />)}
        {activeTab === 'grid'     && (errors.grid    ? <Err msg={errors.grid}    /> : <GridTab    data={grid}    />)}
        {activeTab === 'funding'  && (errors.funding ? <Err msg={errors.funding} /> : <FundingTab data={funding} />)}
        {activeTab === 'flash-arb' && (errors.flashArb ? <Err msg={errors.flashArb} /> : <FlashArbTab data={flashArb} />)}
        {activeTab === 'liquidations' && (errors.liquidations ? <Err msg={errors.liquidations} /> : <LiquidationsTab dataBase={liquidations} dataPolygon={liquidationsPolygon} dataAvax={liquidationsAvax} dataArb={liquidationsArb} />)}
        {activeTab === 'logs'      && <LogsTab />}
        {activeTab === 'system'   && (errors.system  ? <Err msg={errors.system}  /> : <SystemTab  data={system}  />)}
      </main>
    </div>
  );
}
