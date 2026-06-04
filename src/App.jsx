import React, { useState, useEffect, useCallback, useRef } from 'react';
import {
  AreaChart, Area, BarChart, Bar, LineChart, Line,
  XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, ReferenceLine
} from 'recharts';
import {
  Activity, TrendingUp, TrendingDown, Zap, Grid, DollarSign,
  Server, FileText, RefreshCw, Wifi, WifiOff, AlertTriangle,
  CheckCircle, XCircle, Clock, Cpu, HardDrive, MemoryStick,
  ChevronRight, BarChart2, Target, Shield
} from 'lucide-react';

// ─── API client ─────────────────────────────────────────────────────────────
const BASE = '/api-proxy';
const HEADERS = { 'x-api-key': 'JPxK9m2026TraderB0t!', 'Content-Type': 'application/json' };

async function apiFetch(endpoint) {
  const res = await fetch(`${BASE}${endpoint}`, { headers: HEADERS });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

// ─── Helpers ─────────────────────────────────────────────────────────────────
const fmt = (n, decimals = 2) =>
  n == null ? '—' : Number(n).toLocaleString('en-US', { minimumFractionDigits: decimals, maximumFractionDigits: decimals });

const fmtUSD = (n) =>
  n == null ? '—' : (n < 0 ? '-$' : '$') + Math.abs(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

const pct = (n) => n == null ? '—' : `${Number(n).toFixed(2)}%`;

const clr = (n) => n == null ? '' : n >= 0 ? 'positive' : 'negative';

function Loader() {
  return (
    <div className="loading-state">
      <RefreshCw size={18} />
      Loading…
    </div>
  );
}

function ErrBox({ msg }) {
  return <div className="error-state"><AlertTriangle size={14} style={{ marginRight: 6 }} />{msg}</div>;
}

// ─── Custom Tooltip ───────────────────────────────────────────────────────────
function CustomTooltip({ active, payload, label }) {
  if (!active || !payload?.length) return null;
  return (
    <div style={{
      background: '#12122a', border: '1px solid #1a1a33', borderRadius: 8,
      padding: '10px 14px', fontFamily: 'JetBrains Mono, monospace', fontSize: 12
    }}>
      <div style={{ color: '#7070a0', marginBottom: 4 }}>{label}</div>
      {payload.map((p) => (
        <div key={p.dataKey} style={{ color: p.color || '#00ff88' }}>
          {p.name}: <strong>{typeof p.value === 'number' ? fmtUSD(p.value) : p.value}</strong>
        </div>
      ))}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// TAB: OVERVIEW
// ─────────────────────────────────────────────────────────────────────────────
function OverviewTab({ pnl, ibkr, sniper, grid, funding }) {
  if (!pnl) return <Loader />;

  const todayPnl   = pnl.today_pnl   ?? pnl.pnl_today ?? 0;
  const weekPnl    = pnl.week_pnl    ?? pnl.pnl_7d     ?? 0;
  const totalTrades= pnl.total_trades ?? pnl.trades_today ?? '—';
  const winRate    = pnl.win_rate     ?? sniper?.win_rate ?? null;
  const history    = pnl.history      ?? pnl.daily       ?? [];

  const chartData = history.map((d, i) => ({
    day: d.date ?? d.day ?? `D-${history.length - i}`,
    pnl: d.pnl  ?? d.value ?? 0,
  }));

  const regime = ibkr?.regime ?? ibkr?.market_regime ?? 'unknown';
  const regimeClass = regime.toLowerCase().includes('bull') ? 'bull'
    : regime.toLowerCase().includes('bear') ? 'bear'
    : regime.toLowerCase().includes('range') || regime.toLowerCase().includes('sideways') ? 'range'
    : 'unknown';

  return (
    <div>
      {/* KPI row */}
      <div className="grid-4 section-gap">
        <div className="stat-tile">
          <div className="stat-label">P&amp;L Today</div>
          <div className={`stat-value ${todayPnl < 0 ? 'negative' : ''}`}>{fmtUSD(todayPnl)}</div>
          <div className="stat-sub">USD net</div>
        </div>
        <div className="stat-tile">
          <div className="stat-label">P&amp;L 7 Days</div>
          <div className={`stat-value ${weekPnl < 0 ? 'negative' : ''}`}>{fmtUSD(weekPnl)}</div>
          <div className="stat-sub">rolling week</div>
        </div>
        <div className="stat-tile">
          <div className="stat-label">Win Rate</div>
          <div className="stat-value">{winRate != null ? pct(winRate * (winRate <= 1 ? 100 : 1)) : '—'}</div>
          <div className="stat-sub">all strategies</div>
        </div>
        <div className="stat-tile">
          <div className="stat-label">Total Trades</div>
          <div className="stat-value neutral">{totalTrades}</div>
          <div className="stat-sub">today</div>
        </div>
      </div>

      <div className="grid-2 section-gap">
        {/* P&L chart */}
        <div className="card">
          <div className="card-title"><TrendingUp size={14} />7-Day P&amp;L</div>
          {chartData.length > 0 ? (
            <ResponsiveContainer width="100%" height={200}>
              <AreaChart data={chartData}>
                <defs>
                  <linearGradient id="pnlGrad" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%"  stopColor="#00ff88" stopOpacity={0.25} />
                    <stop offset="95%" stopColor="#00ff88" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="#1a1a33" />
                <XAxis dataKey="day" tick={{ fill: '#7070a0', fontSize: 10 }} />
                <YAxis tick={{ fill: '#7070a0', fontSize: 10 }} tickFormatter={(v) => `$${v}`} />
                <Tooltip content={<CustomTooltip />} />
                <ReferenceLine y={0} stroke="#1a1a33" />
                <Area type="monotone" dataKey="pnl" stroke="#00ff88" fill="url(#pnlGrad)" strokeWidth={2} name="P&L" />
              </AreaChart>
            </ResponsiveContainer>
          ) : (
            <div className="empty-state">No history data</div>
          )}
        </div>

        {/* Market regime + quick stats */}
        <div className="card">
          <div className="card-title"><Activity size={14} />Market Regime</div>
          <div style={{ marginBottom: 20 }}>
            <span className={`regime-pill regime-${regimeClass}`}>
              {regimeClass === 'bull' && <TrendingUp size={12} />}
              {regimeClass === 'bear' && <TrendingDown size={12} />}
              {regime || 'Unknown'}
            </span>
          </div>

          <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            {ibkr?.vix != null && (
              <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12 }}>
                <span className="dim mono">VIX</span>
                <span className="mono" style={{ color: ibkr.vix > 25 ? 'var(--yellow)' : 'var(--text)' }}>{fmt(ibkr.vix)}</span>
              </div>
            )}
            {ibkr?.spy_return != null && (
              <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12 }}>
                <span className="dim mono">SPY Return</span>
                <span className={`mono ${clr(ibkr.spy_return)}`}>{pct(ibkr.spy_return)}</span>
              </div>
            )}
            {sniper?.win_rate != null && (
              <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12 }}>
                <span className="dim mono">Sniper Win Rate</span>
                <span className="mono positive">{pct(sniper.win_rate * (sniper.win_rate <= 1 ? 100 : 1))}</span>
              </div>
            )}
            {grid?.active_bots != null && (
              <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12 }}>
                <span className="dim mono">Grid Bots Active</span>
                <span className="mono positive">{grid.active_bots}</span>
              </div>
            )}
            {funding?.positions != null && (
              <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12 }}>
                <span className="dim mono">Funding Positions</span>
                <span className="mono">{funding.positions?.length ?? 0}</span>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// TAB: IBKR
// ─────────────────────────────────────────────────────────────────────────────
function IBKRTab({ data }) {
  if (!data) return <Loader />;

  const signals = data.signals ?? data.ibkr_signals ?? [];
  const regime  = data.regime  ?? data.market_regime ?? 'Unknown';
  const regimeClass = regime.toLowerCase().includes('bull') ? 'bull'
    : regime.toLowerCase().includes('bear') ? 'bear'
    : regime.toLowerCase().includes('range') || regime.toLowerCase().includes('sideways') ? 'range'
    : 'unknown';

  const metrics = [
    { label: 'VIX',        val: data.vix,        fmt: (v) => fmt(v) },
    { label: 'SPY',        val: data.spy,        fmt: (v) => `$${fmt(v)}` },
    { label: 'SPY Return', val: data.spy_return, fmt: (v) => pct(v), cls: clr(data.spy_return) },
    { label: 'QQQ',        val: data.qqq,        fmt: (v) => `$${fmt(v)}` },
    { label: 'IWM',        val: data.iwm,        fmt: (v) => `$${fmt(v)}` },
    { label: 'Beta',       val: data.beta,       fmt: (v) => fmt(v) },
  ].filter(m => m.val != null);

  return (
    <div>
      <div className="grid-4 section-gap">
        <div className="stat-tile" style={{ gridColumn: 'span 1' }}>
          <div className="stat-label">Market Regime</div>
          <div style={{ marginTop: 10 }}>
            <span className={`regime-pill regime-${regimeClass}`}>
              {regimeClass === 'bull' && <TrendingUp size={12} />}
              {regimeClass === 'bear' && <TrendingDown size={12} />}
              {regime}
            </span>
          </div>
        </div>
        {metrics.map((m) => (
          <div key={m.label} className="stat-tile">
            <div className="stat-label">{m.label}</div>
            <div className={`stat-value ${m.cls ?? ''}`} style={{ fontSize: 20 }}>{m.fmt(m.val)}</div>
          </div>
        ))}
      </div>

      <div className="card">
        <div className="card-title"><Zap size={14} />IBKR Signals</div>
        {signals.length === 0 ? (
          <div className="empty-state">No signals at the moment</div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th>Action</th>
                  <th>Price</th>
                  <th>Score</th>
                  <th>Timestamp</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {signals.map((s, i) => (
                  <tr key={i}>
                    <td style={{ fontWeight: 700, color: 'var(--text)' }}>{s.symbol ?? s.ticker ?? '—'}</td>
                    <td>
                      <span className={`badge ${(s.action ?? s.side ?? '').toLowerCase() === 'buy' ? 'badge-green' : 'badge-red'}`}>
                        {s.action ?? s.side ?? '—'}
                      </span>
                    </td>
                    <td>{s.price != null ? `$${fmt(s.price)}` : '—'}</td>
                    <td style={{ color: 'var(--blue)' }}>{s.score != null ? fmt(s.score, 3) : '—'}</td>
                    <td className="dim">{s.timestamp ?? s.time ?? '—'}</td>
                    <td>
                      <span className={`badge ${s.status === 'filled' ? 'badge-green' : s.status === 'pending' ? 'badge-yellow' : 'badge-blue'}`}>
                        {s.status ?? 'new'}
                      </span>
                    </td>
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

// ─────────────────────────────────────────────────────────────────────────────
// TAB: SNIPER
// ─────────────────────────────────────────────────────────────────────────────
function SniperTab({ data }) {
  if (!data) return <Loader />;

  const trades   = data.trades   ?? data.history  ?? [];
  const winRate  = data.win_rate ?? null;
  const totalPnl = data.total_pnl ?? data.pnl ?? null;
  const totalTrades = data.total_trades ?? trades.length;
  const avgPnl   = data.avg_pnl  ?? null;

  const chartData = trades.slice(-20).map((t, i) => ({
    idx: i + 1,
    pnl: t.pnl ?? t.profit ?? 0,
  }));

  return (
    <div>
      <div className="grid-4 section-gap">
        <div className="stat-tile">
          <div className="stat-label">Win Rate</div>
          <div className="stat-value">{winRate != null ? pct(winRate * (winRate <= 1 ? 100 : 1)) : '—'}</div>
        </div>
        <div className="stat-tile">
          <div className="stat-label">Total P&amp;L</div>
          <div className={`stat-value ${totalPnl != null && totalPnl < 0 ? 'negative' : ''}`}>{fmtUSD(totalPnl)}</div>
        </div>
        <div className="stat-tile">
          <div className="stat-label">Total Trades</div>
          <div className="stat-value neutral">{totalTrades}</div>
        </div>
        <div className="stat-tile">
          <div className="stat-label">Avg P&amp;L / Trade</div>
          <div className={`stat-value ${avgPnl != null && avgPnl < 0 ? 'negative' : ''}`}>{avgPnl != null ? fmtUSD(avgPnl) : '—'}</div>
        </div>
      </div>

      {chartData.length > 0 && (
        <div className="card section-gap">
          <div className="card-title"><BarChart2 size={14} />Recent Trades P&amp;L</div>
          <ResponsiveContainer width="100%" height={180}>
            <BarChart data={chartData}>
              <CartesianGrid strokeDasharray="3 3" stroke="#1a1a33" />
              <XAxis dataKey="idx" tick={{ fill: '#7070a0', fontSize: 10 }} />
              <YAxis tick={{ fill: '#7070a0', fontSize: 10 }} tickFormatter={(v) => `$${v}`} />
              <Tooltip content={<CustomTooltip />} />
              <ReferenceLine y={0} stroke="#1a1a33" />
              <Bar dataKey="pnl" name="P&L"
                fill="#00ff88"
                radius={[3, 3, 0, 0]}
              />
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}

      <div className="card">
        <div className="card-title"><Target size={14} />Trade History — BSC Sniper</div>
        {trades.length === 0 ? (
          <div className="empty-state">No trades yet</div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Token</th>
                  <th>Side</th>
                  <th>Entry</th>
                  <th>Exit</th>
                  <th>P&amp;L</th>
                  <th>Status</th>
                  <th>Time</th>
                </tr>
              </thead>
              <tbody>
                {[...trades].reverse().slice(0, 50).map((t, i) => (
                  <tr key={i}>
                    <td style={{ fontWeight: 700 }}>{t.token ?? t.symbol ?? t.pair ?? '—'}</td>
                    <td>
                      <span className={`badge ${(t.side ?? t.action ?? '').toLowerCase() === 'buy' ? 'badge-green' : 'badge-red'}`}>
                        {t.side ?? t.action ?? '—'}
                      </span>
                    </td>
                    <td className="mono">{t.entry  != null ? `$${fmt(t.entry,  4)}` : '—'}</td>
                    <td className="mono">{t.exit   != null ? `$${fmt(t.exit,   4)}` : '—'}</td>
                    <td className={`mono ${clr(t.pnl ?? t.profit)}`}>
                      {fmtUSD(t.pnl ?? t.profit)}
                    </td>
                    <td>
                      <span className={`badge ${t.status === 'closed' || t.status === 'filled' ? 'badge-green' : t.status === 'open' ? 'badge-blue' : 'badge-yellow'}`}>
                        {t.status ?? '—'}
                      </span>
                    </td>
                    <td className="dim">{t.timestamp ?? t.time ?? '—'}</td>
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

// ─────────────────────────────────────────────────────────────────────────────
// TAB: GRID
// ─────────────────────────────────────────────────────────────────────────────
function GridTab({ data }) {
  if (!data) return <Loader />;

  const bots   = data.bots   ?? data.active_bots_list  ?? data.grids  ?? [];
  const trades = data.trades ?? data.recent_trades      ?? [];

  return (
    <div>
      <div className="grid-3 section-gap">
        <div className="stat-tile">
          <div className="stat-label">Active Bots</div>
          <div className="stat-value neutral">{data.active_bots ?? bots.length}</div>
        </div>
        <div className="stat-tile">
          <div className="stat-label">Total P&amp;L</div>
          <div className={`stat-value ${(data.total_pnl ?? 0) < 0 ? 'negative' : ''}`}>{fmtUSD(data.total_pnl ?? data.pnl)}</div>
        </div>
        <div className="stat-tile">
          <div className="stat-label">Grid Trades</div>
          <div className="stat-value neutral">{data.total_trades ?? trades.length}</div>
        </div>
      </div>

      <div className="card section-gap">
        <div className="card-title"><Grid size={14} />Active Grid Bots</div>
        {bots.length === 0 ? (
          <div className="empty-state">No active bots</div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Pair</th>
                  <th>Lower</th>
                  <th>Upper</th>
                  <th>Grids</th>
                  <th>Invested</th>
                  <th>P&amp;L</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {bots.map((b, i) => (
                  <tr key={i}>
                    <td style={{ fontWeight: 700 }}>{b.pair ?? b.symbol ?? '—'}</td>
                    <td className="mono dim">{b.lower  != null ? `$${fmt(b.lower,  4)}` : '—'}</td>
                    <td className="mono dim">{b.upper  != null ? `$${fmt(b.upper,  4)}` : '—'}</td>
                    <td className="mono">{b.grids ?? b.grid_count ?? '—'}</td>
                    <td className="mono">{b.invested != null ? fmtUSD(b.invested) : '—'}</td>
                    <td className={`mono ${clr(b.pnl ?? b.profit)}`}>{fmtUSD(b.pnl ?? b.profit)}</td>
                    <td>
                      <span className={`badge ${b.status === 'running' || b.status === 'active' ? 'badge-green' : 'badge-yellow'}`}>
                        {b.status ?? 'active'}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {trades.length > 0 && (
        <div className="card">
          <div className="card-title"><Activity size={14} />Recent Grid Trades</div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Pair</th>
                  <th>Side</th>
                  <th>Price</th>
                  <th>Qty</th>
                  <th>P&amp;L</th>
                  <th>Time</th>
                </tr>
              </thead>
              <tbody>
                {trades.slice(0, 30).map((t, i) => (
                  <tr key={i}>
                    <td style={{ fontWeight: 700 }}>{t.pair ?? t.symbol ?? '—'}</td>
                    <td>
                      <span className={`badge ${(t.side ?? '').toLowerCase() === 'buy' ? 'badge-green' : 'badge-red'}`}>
                        {t.side ?? '—'}
                      </span>
                    </td>
                    <td className="mono">{t.price != null ? `$${fmt(t.price, 4)}` : '—'}</td>
                    <td className="mono">{t.qty   != null ? fmt(t.qty, 4) : '—'}</td>
                    <td className={`mono ${clr(t.pnl)}`}>{fmtUSD(t.pnl)}</td>
                    <td className="dim">{t.timestamp ?? t.time ?? '—'}</td>
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

// ─────────────────────────────────────────────────────────────────────────────
// TAB: FUNDING
// ─────────────────────────────────────────────────────────────────────────────
function FundingTab({ data }) {
  if (!data) return <Loader />;

  const positions = data.positions ?? data.funding_positions ?? [];
  const totalPnl  = data.total_pnl  ?? data.pnl ?? null;
  const totalFund = data.total_funding ?? data.earned ?? null;

  return (
    <div>
      <div className="grid-3 section-gap">
        <div className="stat-tile">
          <div className="stat-label">Open Positions</div>
          <div className="stat-value neutral">{positions.length}</div>
        </div>
        <div className="stat-tile">
          <div className="stat-label">Total P&amp;L</div>
          <div className={`stat-value ${totalPnl != null && totalPnl < 0 ? 'negative' : ''}`}>{fmtUSD(totalPnl)}</div>
        </div>
        <div className="stat-tile">
          <div className="stat-label">Funding Earned</div>
          <div className="stat-value">{fmtUSD(totalFund)}</div>
        </div>
      </div>

      <div className="card">
        <div className="card-title"><DollarSign size={14} />Funding Rate Positions</div>
        {positions.length === 0 ? (
          <div className="empty-state">No open positions</div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th>Side</th>
                  <th>Size</th>
                  <th>Entry</th>
                  <th>Mark</th>
                  <th>Funding Rate</th>
                  <th>Funding 8h</th>
                  <th>Unr. P&amp;L</th>
                </tr>
              </thead>
              <tbody>
                {positions.map((p, i) => {
                  const rate = p.funding_rate ?? p.rate ?? 0;
                  const pct8 = rate * 100;
                  const barPct = Math.min(Math.abs(pct8) * 10, 50);
                  return (
                    <tr key={i}>
                      <td style={{ fontWeight: 700 }}>{p.symbol ?? p.pair ?? '—'}</td>
                      <td>
                        <span className={`badge ${(p.side ?? '').toLowerCase() === 'long' ? 'badge-green' : 'badge-red'}`}>
                          {p.side ?? '—'}
                        </span>
                      </td>
                      <td className="mono">{p.size  != null ? fmt(p.size) : '—'}</td>
                      <td className="mono">{p.entry != null ? `$${fmt(p.entry)}` : '—'}</td>
                      <td className="mono">{p.mark  != null ? `$${fmt(p.mark)}` : '—'}</td>
                      <td>
                        <div className="funding-bar-wrap">
                          <span className={`mono ${rate >= 0 ? 'positive' : 'negative'}`} style={{ minWidth: 60 }}>
                            {pct(pct8)}
                          </span>
                          <div className="funding-bar" style={{ width: 60 }}>
                            {rate >= 0
                              ? <div className="funding-fill-pos" style={{ width: `${barPct}%` }} />
                              : <div className="funding-fill-neg" style={{ width: `${barPct}%` }} />}
                          </div>
                        </div>
                      </td>
                      <td className={`mono ${clr(p.funding_earned ?? p.funding_pnl)}`}>
                        {fmtUSD(p.funding_earned ?? p.funding_pnl)}
                      </td>
                      <td className={`mono ${clr(p.unrealized_pnl ?? p.unr_pnl)}`}>
                        {fmtUSD(p.unrealized_pnl ?? p.unr_pnl)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// TAB: LOGS
// ─────────────────────────────────────────────────────────────────────────────
const LOG_SERVICES = ['ibkr', 'sniper', 'grid', 'funding', 'system', 'api'];

function parseLevel(line) {
  if (/ERROR|CRITICAL|FATAL/i.test(line)) return 'ERROR';
  if (/WARN/i.test(line))  return 'WARN';
  if (/DEBUG/i.test(line)) return 'DEBUG';
  return 'INFO';
}

function LogsTab() {
  const [service, setService] = useState('ibkr');
  const [lines, setLines]     = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError]     = useState(null);
  const bottomRef = useRef(null);

  const fetchLogs = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await apiFetch(`/api/logs/${service}`);
      const raw = data.logs ?? data.lines ?? data.content ?? (typeof data === 'string' ? data.split('\n') : []);
      setLines(Array.isArray(raw) ? raw : raw.split('\n'));
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [service]);

  useEffect(() => { fetchLogs(); }, [fetchLogs]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [lines]);

  return (
    <div>
      <div className="card">
        <div className="card-title" style={{ justifyContent: 'space-between' }}>
          <span><FileText size={14} />Log Viewer</span>
          <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
            <select className="log-select" value={service} onChange={(e) => setService(e.target.value)}>
              {LOG_SERVICES.map((s) => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
            <button className={`refresh-btn ${loading ? 'spinning' : ''}`} onClick={fetchLogs}>
              <RefreshCw size={12} />Refresh
            </button>
          </div>
        </div>

        {error ? <ErrBox msg={error} /> : (
          <div className="log-console">
            {lines.length === 0 && !loading && <span className="dim">No log lines</span>}
            {lines.map((line, i) => {
              const level = parseLevel(line);
              const tsMatch = line.match(/^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})/);
              const ts = tsMatch ? tsMatch[1] : null;
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
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// TAB: SYSTEM
// ─────────────────────────────────────────────────────────────────────────────
function SystemTab({ data }) {
  if (!data) return <Loader />;

  const cpu    = data.cpu    ?? data.cpu_percent    ?? null;
  const ram    = data.ram    ?? data.memory_percent ?? data.ram_percent ?? null;
  const disk   = data.disk   ?? data.disk_percent   ?? null;
  const uptime = data.uptime ?? null;

  const services = data.services ?? data.service_status ?? {};

  const gauges = [
    { label: 'CPU',  value: cpu,  unit: '%' },
    { label: 'RAM',  value: ram,  unit: '%' },
    { label: 'Disk', value: disk, unit: '%' },
  ].filter((g) => g.value != null);

  function gaugeClass(v) {
    if (v >= 90) return 'danger';
    if (v >= 70) return 'warn';
    return '';
  }

  const serviceList = Object.entries(services);

  return (
    <div>
      <div className="grid-3 section-gap">
        {gauges.map((g) => (
          <div key={g.label} className="card">
            <div className="card-title">
              {g.label === 'CPU'  && <Cpu size={14} />}
              {g.label === 'RAM'  && <MemoryStick size={14} />}
              {g.label === 'Disk' && <HardDrive size={14} />}
              {g.label}
            </div>
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginBottom: 10 }}>
              <span className="mono" style={{ fontSize: 28, fontWeight: 700, color: g.value >= 90 ? 'var(--red)' : g.value >= 70 ? 'var(--yellow)' : 'var(--green)' }}>
                {fmt(g.value, 1)}{g.unit}
              </span>
            </div>
            <div className="progress-bar">
              <div className={`progress-fill ${gaugeClass(g.value)}`} style={{ width: `${Math.min(g.value, 100)}%` }} />
            </div>
          </div>
        ))}
      </div>

      {uptime && (
        <div className="card section-gap" style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <Clock size={14} style={{ color: 'var(--text-dim)' }} />
          <span className="dim" style={{ fontSize: 12 }}>Uptime:</span>
          <span className="mono" style={{ fontSize: 13 }}>{uptime}</span>
        </div>
      )}

      <div className="card">
        <div className="card-title"><Server size={14} />Services</div>
        {serviceList.length === 0 ? (
          <div className="empty-state">No service data</div>
        ) : (
          serviceList.map(([name, status]) => {
            const isUp = (status === true || status === 'running' || status === 'up' || status === 'active' || status === 'ok');
            return (
              <div key={name} className="service-item">
                <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                  {isUp
                    ? <CheckCircle size={14} style={{ color: 'var(--green)' }} />
                    : <XCircle    size={14} style={{ color: 'var(--red)'   }} />}
                  <span className="service-name">{name}</span>
                </div>
                <span className={`badge ${isUp ? 'badge-green' : 'badge-red'}`}>
                  {typeof status === 'string' ? status : isUp ? 'running' : 'down'}
                </span>
              </div>
            );
          })
        )}
      </div>

      {data.load_avg != null && (
        <div className="card" style={{ marginTop: 16 }}>
          <div className="card-title"><Activity size={14} />Load Average</div>
          <div style={{ display: 'flex', gap: 32 }}>
            {(Array.isArray(data.load_avg) ? data.load_avg : [data.load_avg]).map((v, i) => (
              <div key={i}>
                <div className="dim mono" style={{ fontSize: 10 }}>{['1m', '5m', '15m'][i] ?? `${i}`}</div>
                <div className="mono" style={{ fontSize: 20, color: 'var(--green)' }}>{fmt(v, 2)}</div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// ROOT APP
// ─────────────────────────────────────────────────────────────────────────────
const TABS = [
  { id: 'overview', label: 'Overview',  icon: BarChart2   },
  { id: 'ibkr',     label: 'IBKR',      icon: TrendingUp  },
  { id: 'sniper',   label: 'Sniper',    icon: Target      },
  { id: 'grid',     label: 'Grid',      icon: Grid        },
  { id: 'funding',  label: 'Funding',   icon: DollarSign  },
  { id: 'logs',     label: 'Logs',      icon: FileText    },
  { id: 'system',   label: 'System',    icon: Server      },
];

export default function App() {
  const [activeTab, setActiveTab] = useState('overview');
  const [loading, setLoading]     = useState(false);
  const [online, setOnline]       = useState(null);
  const [lastUpdate, setLastUpdate] = useState(null);

  const [pnl,     setPnl]     = useState(null);
  const [ibkr,    setIbkr]    = useState(null);
  const [sniper,  setSniper]  = useState(null);
  const [grid,    setGrid]    = useState(null);
  const [funding, setFunding] = useState(null);
  const [system,  setSystem]  = useState(null);

  const fetchAll = useCallback(async () => {
    setLoading(true);
    try {
      const [pnlData, ibkrData, sniperData, gridData, fundingData, systemData] = await Promise.allSettled([
        apiFetch('/api/pnl'),
        apiFetch('/api/ibkr'),
        apiFetch('/api/sniper'),
        apiFetch('/api/grid'),
        apiFetch('/api/funding'),
        apiFetch('/api/system'),
      ]);
      if (pnlData.status     === 'fulfilled') setPnl(pnlData.value);
      if (ibkrData.status    === 'fulfilled') setIbkr(ibkrData.value);
      if (sniperData.status  === 'fulfilled') setSniper(sniperData.value);
      if (gridData.status    === 'fulfilled') setGrid(gridData.value);
      if (fundingData.status === 'fulfilled') setFunding(fundingData.value);
      if (systemData.status  === 'fulfilled') setSystem(systemData.value);
      setOnline(true);
      setLastUpdate(new Date().toLocaleTimeString());
    } catch {
      setOnline(false);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchAll();
    const id = setInterval(fetchAll, 30_000);
    return () => clearInterval(id);
  }, [fetchAll]);

  return (
    <div className="app">
      {/* Header */}
      <header className="header">
        <div className="header-logo">
          <div className="header-logo-icon">
            <Activity size={18} />
          </div>
          <div>
            <div className="header-title">TRADER DASHBOARD</div>
            <div className="header-subtitle">Algorithmic Trading Monitor</div>
          </div>
        </div>

        <div className="header-right">
          {lastUpdate && (
            <div className="status-pill">
              <Clock size={11} />
              {lastUpdate}
            </div>
          )}
          <div className="status-pill">
            <div className={`status-dot ${online === false ? 'offline' : ''}`} />
            {online === null ? 'Connecting…' : online ? 'Online' : 'Offline'}
          </div>
          <button className={`refresh-btn ${loading ? 'spinning' : ''}`} onClick={fetchAll}>
            <RefreshCw size={12} />
            Refresh
          </button>
        </div>
      </header>

      {/* Tabs */}
      <nav className="tabs">
        {TABS.map((t) => {
          const Icon = t.icon;
          return (
            <button
              key={t.id}
              className={`tab ${activeTab === t.id ? 'active' : ''}`}
              onClick={() => setActiveTab(t.id)}
            >
              <Icon size={14} />
              {t.label}
            </button>
          );
        })}
      </nav>

      {/* Content */}
      <main className="content">
        {activeTab === 'overview' && <OverviewTab pnl={pnl} ibkr={ibkr} sniper={sniper} grid={grid} funding={funding} />}
        {activeTab === 'ibkr'     && <IBKRTab    data={ibkr} />}
        {activeTab === 'sniper'   && <SniperTab  data={sniper} />}
        {activeTab === 'grid'     && <GridTab    data={grid} />}
        {activeTab === 'funding'  && <FundingTab data={funding} />}
        {activeTab === 'logs'     && <LogsTab />}
        {activeTab === 'system'   && <SystemTab  data={system} />}
      </main>
    </div>
  );
}
