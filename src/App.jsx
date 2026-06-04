import React, { useState, useEffect, useCallback, useRef } from 'react';
import {
  AreaChart, Area, BarChart, Bar,
  XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, ReferenceLine
} from 'recharts';
import {
  Activity, TrendingUp, TrendingDown, Zap, Grid, DollarSign,
  Server, FileText, RefreshCw, AlertTriangle,
  CheckCircle, XCircle, Clock, Cpu, HardDrive, MemoryStick,
  BarChart2, Target, Shield
} from 'lucide-react';

// ─── API client ──────────────────────────────────────────────────────────────
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

const fmtBNB = (n) =>
  n == null ? '—' : (n < 0 ? '-' : '') + Math.abs(Number(n)).toFixed(6) + ' BNB';

const pct = (n) => n == null ? '—' : `${Number(n).toFixed(2)}%`;

const clr = (n) => n == null ? '' : Number(n) >= 0 ? 'positive' : 'negative';

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
          {p.name}: <strong>{p.value}</strong>
        </div>
      ))}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// TAB: OVERVIEW
// API: { today: {sniper_bnb, sniper_trades, sniper_win_rate, grid_usdt, grid_trades},
//        history_7d: [{date, sniper_bnb, grid_usdt, ibkr_usd}] }
// ─────────────────────────────────────────────────────────────────────────────
function OverviewTab({ pnl, ibkr, sniper, grid }) {
  if (!pnl) return <Loader />;

  const todayBNB   = pnl.today?.sniper_bnb   ?? 0;
  const todayUSDT  = pnl.today?.grid_usdt    ?? 0;
  const totalTrades= (pnl.today?.sniper_trades ?? 0) + (pnl.today?.grid_trades ?? 0);
  const winRate    = pnl.today?.sniper_win_rate ?? sniper?.win_rate ?? 0;

  const history   = pnl.history_7d ?? [];
  const chartData = history.map((d) => ({
    day: (d.date ?? '').slice(5),
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
      <div className="grid-4 section-gap">
        <div className="stat-tile">
          <div className="stat-label">Sniper P&amp;L Today</div>
          <div className={`stat-value ${todayBNB < 0 ? 'negative' : ''}`} style={{ fontSize: 20 }}>
            {fmtBNB(todayBNB)}
          </div>
          <div className="stat-sub">BSC net</div>
        </div>
        <div className="stat-tile">
          <div className="stat-label">Grid P&amp;L Today</div>
          <div className={`stat-value ${todayUSDT < 0 ? 'negative' : ''}`} style={{ fontSize: 20 }}>
            {fmtUSD(todayUSDT)}
          </div>
          <div className="stat-sub">USDT net</div>
        </div>
        <div className="stat-tile">
          <div className="stat-label">Win Rate (Sniper)</div>
          <div className="stat-value">{pct(winRate)}</div>
          <div className="stat-sub">today</div>
        </div>
        <div className="stat-tile">
          <div className="stat-label">Total Trades</div>
          <div className="stat-value neutral">{totalTrades}</div>
          <div className="stat-sub">sniper + grid hoje</div>
        </div>
      </div>

      <div className="grid-2 section-gap">
        <div className="card">
          <div className="card-title"><TrendingUp size={14} />7-Day P&amp;L History</div>
          {chartData.length > 0 ? (
            <ResponsiveContainer width="100%" height={200}>
              <BarChart data={chartData}>
                <CartesianGrid strokeDasharray="3 3" stroke="#1a1a33" />
                <XAxis dataKey="day" tick={{ fill: '#7070a0', fontSize: 10 }} />
                <YAxis tick={{ fill: '#7070a0', fontSize: 10 }} />
                <Tooltip content={<CustomTooltip />} />
                <ReferenceLine y={0} stroke="#333360" />
                <Bar dataKey="sniper" name="Sniper BNB" fill="#00ff88" radius={[3,3,0,0]} />
                <Bar dataKey="grid"   name="Grid USDT"  fill="#4488ff" radius={[3,3,0,0]} />
              </BarChart>
            </ResponsiveContainer>
          ) : (
            <div className="empty-state">Sem histórico</div>
          )}
        </div>

        <div className="card">
          <div className="card-title"><Activity size={14} />Resumo Semanal</div>
          <div style={{ marginBottom: 16 }}>
            <span className={`regime-pill regime-${regimeClass}`}>
              {regimeClass === 'bull' && <TrendingUp size={12} />}
              {regimeClass === 'bear' && <TrendingDown size={12} />}
              {regime}
            </span>
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12 }}>
              <span className="dim mono">Sniper 7d</span>
              <span className={`mono ${clr(weekSniper)}`}>{fmtBNB(weekSniper)}</span>
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12 }}>
              <span className="dim mono">Grid 7d</span>
              <span className={`mono ${clr(weekGrid)}`}>{fmtUSD(weekGrid)}</span>
            </div>
            {ibkr?.regime?.vix != null && (
              <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12 }}>
                <span className="dim mono">VIX</span>
                <span className="mono" style={{ color: ibkr.regime.vix > 25 ? 'var(--yellow)' : 'var(--text)' }}>
                  {fmt(ibkr.regime.vix)}
                </span>
              </div>
            )}
            {ibkr?.regime?.spy != null && (
              <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12 }}>
                <span className="dim mono">SPY</span>
                <span className="mono">${fmt(ibkr.regime.spy)}</span>
              </div>
            )}
            {ibkr?.signals?.signals_today != null && (
              <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12 }}>
                <span className="dim mono">IBKR Signals Hoje</span>
                <span className="mono positive">{ibkr.signals.signals_today}</span>
              </div>
            )}
            {grid?.active_bots != null && (
              <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12 }}>
                <span className="dim mono">Grid Bots Activos</span>
                <span className="mono positive">{grid.active_bots}</span>
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
// API: { account, mode, regime: {regime, spy, vix}, signals: {signals_today, orders_placed, recent_signals: [{strategy, ticker, direction, price, stop_loss, take_profit, score, timestamp}]} }
// ─────────────────────────────────────────────────────────────────────────────
function IBKRTab({ data }) {
  if (!data) return <Loader />;

  const regime   = String(data.regime?.regime ?? 'UNKNOWN');
  const vix      = data.regime?.vix  ?? null;
  const spy      = data.regime?.spy  ?? null;
  const signals  = data.signals?.recent_signals ?? [];
  const sigCount = data.signals?.signals_today  ?? signals.length;
  const orders   = data.signals?.orders_placed  ?? 0;

  const regimeClass = regime.toLowerCase().includes('bull') ? 'bull'
    : regime.toLowerCase().includes('bear') ? 'bear'
    : regime.toLowerCase().includes('range') ? 'range'
    : 'unknown';

  return (
    <div>
      <div className="grid-4 section-gap">
        <div className="stat-tile">
          <div className="stat-label">Regime</div>
          <div style={{ marginTop: 10 }}>
            <span className={`regime-pill regime-${regimeClass}`}>
              {regimeClass === 'bull' && <TrendingUp size={12} />}
              {regimeClass === 'bear' && <TrendingDown size={12} />}
              {regime}
            </span>
          </div>
        </div>
        <div className="stat-tile">
          <div className="stat-label">VIX</div>
          <div className="stat-value" style={{ fontSize: 22, color: vix > 25 ? 'var(--yellow)' : 'var(--green)' }}>
            {vix != null ? fmt(vix) : '—'}
          </div>
        </div>
        <div className="stat-tile">
          <div className="stat-label">SPY</div>
          <div className="stat-value neutral" style={{ fontSize: 22 }}>
            {spy != null ? `$${fmt(spy)}` : '—'}
          </div>
        </div>
        <div className="stat-tile">
          <div className="stat-label">Sinais Hoje</div>
          <div className="stat-value neutral">{sigCount}</div>
          <div className="stat-sub">{orders} ordens executadas</div>
        </div>
      </div>

      <div className="card">
        <div className="card-title"><Zap size={14} />Sinais Recentes — {data.account} ({data.mode})</div>
        {signals.length === 0 ? (
          <div className="empty-state">Sem sinais recentes</div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Ticker</th>
                  <th>Direção</th>
                  <th>Preço</th>
                  <th>Stop Loss</th>
                  <th>Take Profit</th>
                  <th>Score</th>
                  <th>Estratégia</th>
                  <th>Timestamp</th>
                </tr>
              </thead>
              <tbody>
                {signals.map((s, i) => (
                  <tr key={i}>
                    <td style={{ fontWeight: 700 }}>{s.ticker ?? '—'}</td>
                    <td>
                      <span className={`badge ${(s.direction ?? '').toLowerCase() === 'long' ? 'badge-green' : 'badge-red'}`}>
                        {s.direction ?? '—'}
                      </span>
                    </td>
                    <td className="mono">{s.price   != null ? `$${fmt(s.price,   2)}` : '—'}</td>
                    <td className="mono negative">{s.stop_loss   != null ? `$${fmt(s.stop_loss,   2)}` : '—'}</td>
                    <td className="mono positive">{s.take_profit != null ? `$${fmt(s.take_profit, 2)}` : '—'}</td>
                    <td style={{ color: 'var(--blue)' }}>{s.score ?? '—'}</td>
                    <td className="dim">{s.strategy ?? '—'}</td>
                    <td className="dim" style={{ fontSize: 11 }}>{s.timestamp ?? '—'}</td>
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
// API: { total_pnl_bnb, wins, losses, win_rate, total_trades,
//        recent_trades: [{type, side, token, pnl, timestamp}],
//        pnl_history: [{date, pnl_bnb, trades, win_rate}] }
// ─────────────────────────────────────────────────────────────────────────────
function SniperTab({ data }) {
  if (!data) return <Loader />;

  const trades    = data.recent_trades   ?? [];
  const history   = data.pnl_history     ?? [];
  const totalBNB  = data.total_pnl_bnb   ?? 0;
  const totalUSDT = data.total_pnl_usdt  ?? 0;
  const winRate   = data.win_rate        ?? 0;
  const wins      = data.wins            ?? 0;
  const losses    = data.losses          ?? 0;

  const chartData = history.map((d) => ({
    day:  (d.date ?? '').slice(5),
    bnb:  Number(d.pnl_bnb  ?? 0),
    usdt: Number(d.pnl_usdt ?? 0),
  }));

  return (
    <div>
      <div className="grid-4 section-gap">
        <div className="stat-tile">
          <div className="stat-label">Win Rate</div>
          <div className="stat-value">{pct(winRate)}</div>
          <div className="stat-sub">{wins}W / {losses}L</div>
        </div>
        <div className="stat-tile">
          <div className="stat-label">P&amp;L BNB (7d)</div>
          <div className={`stat-value ${totalBNB < 0 ? 'negative' : ''}`} style={{ fontSize: 18 }}>
            {fmtBNB(totalBNB)}
          </div>
        </div>
        <div className="stat-tile">
          <div className="stat-label">P&amp;L USDT (7d)</div>
          <div className={`stat-value ${totalUSDT < 0 ? 'negative' : ''}`} style={{ fontSize: 18 }}>
            {fmtUSD(totalUSDT)}
          </div>
        </div>
        <div className="stat-tile">
          <div className="stat-label">Total Trades</div>
          <div className="stat-value neutral">{data.total_trades ?? 0}</div>
        </div>
      </div>

      {chartData.length > 0 && (
        <div className="card section-gap">
          <div className="card-title"><BarChart2 size={14} />P&amp;L 7 dias</div>
          <ResponsiveContainer width="100%" height={180}>
            <BarChart data={chartData}>
              <CartesianGrid strokeDasharray="3 3" stroke="#1a1a33" />
              <XAxis dataKey="day" tick={{ fill: '#7070a0', fontSize: 10 }} />
              <YAxis tick={{ fill: '#7070a0', fontSize: 10 }} />
              <Tooltip content={<CustomTooltip />} />
              <ReferenceLine y={0} stroke="#333360" />
              <Bar dataKey="bnb"  name="BNB"  fill="#00ff88" radius={[3,3,0,0]} />
              <Bar dataKey="usdt" name="USDT" fill="#4488ff" radius={[3,3,0,0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}

      <div className="card">
        <div className="card-title"><Target size={14} />Trades Recentes — BSC Sniper</div>
        {trades.length === 0 ? (
          <div className="empty-state">Sem trades recentes</div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Token</th>
                  <th>Resultado</th>
                  <th>P&amp;L</th>
                  <th>Moeda</th>
                  <th>Timestamp</th>
                </tr>
              </thead>
              <tbody>
                {[...trades].reverse().map((t, i) => (
                  <tr key={i}>
                    <td style={{ fontWeight: 700, fontSize: 11 }}>{t.token ?? '—'}</td>
                    <td>
                      <span className={`badge ${t.type === 'TAKE_PROFIT' ? 'badge-green' : 'badge-red'}`}>
                        {t.type === 'TAKE_PROFIT' ? 'TP' : t.type === 'STOP_LOSS' ? 'SL' : t.type ?? '—'}
                      </span>
                    </td>
                    <td className={`mono ${clr(t.pnl)}`}>
                      {t.pnl != null ? (t.pnl >= 0 ? '+' : '') + Number(t.pnl).toFixed(6) : '—'}
                    </td>
                    <td>
                      <span className={`badge ${t.currency === 'WBNB' ? 'badge-yellow' : 'badge-blue'}`}>
                        {t.currency ?? '—'}
                      </span>
                    </td>
                    <td className="dim" style={{ fontSize: 11 }}>{t.timestamp ?? '—'}</td>
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
// API: { total_pnl, total_trades, recent_trades: [{side, pair, price, pnl, timestamp}],
//        active_bots, active_grids: [{bot, pair, price, range, levels, lower, upper, status}] }
// ─────────────────────────────────────────────────────────────────────────────
function GridTab({ data }) {
  if (!data) return <Loader />;

  const bots   = data.active_grids ?? [];
  const trades = data.recent_trades ?? [];

  return (
    <div>
      <div className="grid-3 section-gap">
        <div className="stat-tile">
          <div className="stat-label">Bots Activos</div>
          <div className="stat-value neutral">{data.active_bots ?? bots.length}</div>
        </div>
        <div className="stat-tile">
          <div className="stat-label">Total P&amp;L</div>
          <div className={`stat-value ${(data.total_pnl ?? 0) < 0 ? 'negative' : ''}`}>
            {fmtUSD(data.total_pnl)}
          </div>
        </div>
        <div className="stat-tile">
          <div className="stat-label">Grid Trades</div>
          <div className="stat-value neutral">{data.total_trades ?? trades.length}</div>
        </div>
      </div>

      <div className="card section-gap">
        <div className="card-title"><Grid size={14} />Grid Bots Activos</div>
        {bots.length === 0 ? (
          <div className="empty-state">Sem bots activos detectados nos logs</div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Bot</th>
                  <th>Par</th>
                  <th>Preço</th>
                  <th>Lower</th>
                  <th>Upper</th>
                  <th>Range</th>
                  <th>Níveis</th>
                  <th>Estado</th>
                </tr>
              </thead>
              <tbody>
                {bots.map((b, i) => (
                  <tr key={i}>
                    <td className="dim" style={{ fontSize: 11 }}>{b.bot ?? '—'}</td>
                    <td style={{ fontWeight: 700 }}>{b.pair ?? '—'}</td>
                    <td className="mono">{b.price != null ? fmt(b.price, 4) : '—'}</td>
                    <td className="mono dim">{b.lower != null ? fmt(b.lower, 4) : '—'}</td>
                    <td className="mono dim">{b.upper != null ? fmt(b.upper, 4) : '—'}</td>
                    <td className="mono">{b.range ?? '—'}</td>
                    <td className="mono">{b.levels ?? '—'}</td>
                    <td><span className="badge badge-yellow">dry_run</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {trades.length > 0 && (
        <div className="card">
          <div className="card-title"><Activity size={14} />Trades Recentes</div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Par</th>
                  <th>Side</th>
                  <th>Preço</th>
                  <th>P&amp;L</th>
                  <th>Timestamp</th>
                </tr>
              </thead>
              <tbody>
                {trades.slice(0, 30).map((t, i) => (
                  <tr key={i}>
                    <td style={{ fontWeight: 700 }}>{t.pair ?? '—'}</td>
                    <td>
                      <span className={`badge ${(t.side ?? '').toLowerCase() === 'buy' ? 'badge-green' : 'badge-red'}`}>
                        {t.side ?? '—'}
                      </span>
                    </td>
                    <td className="mono">{t.price != null ? fmt(t.price, 4) : '—'}</td>
                    <td className={`mono ${clr(t.pnl)}`}>{t.pnl != null ? fmtUSD(t.pnl) : 'dry_run'}</td>
                    <td className="dim" style={{ fontSize: 11 }}>{t.timestamp ?? '—'}</td>
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
// API: { active_positions, positions: [{symbol, side, size, entry, mark, funding_rate, funding_earned, unrealized_pnl}],
//        total_earned_usdt }
// ─────────────────────────────────────────────────────────────────────────────
function FundingTab({ data }) {
  if (!data) return <Loader />;

  const positions   = data.positions         ?? [];
  const totalEarned = data.total_earned_usdt ?? null;

  return (
    <div>
      <div className="grid-2 section-gap">
        <div className="stat-tile">
          <div className="stat-label">Posições Abertas</div>
          <div className="stat-value neutral">{data.active_positions ?? positions.length}</div>
        </div>
        <div className="stat-tile">
          <div className="stat-label">Funding Earned</div>
          <div className="stat-value">{fmtUSD(totalEarned)}</div>
        </div>
      </div>

      <div className="card">
        <div className="card-title"><DollarSign size={14} />Funding Rate Positions</div>
        {positions.length === 0 ? (
          <div className="empty-state">Sem posições abertas detectadas nos logs</div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th>Side</th>
                  <th>Size (USDT/ordem)</th>
                  <th>Lower</th>
                  <th>Upper</th>
                  <th>Funding Earned</th>
                  <th>Unr. P&amp;L</th>
                </tr>
              </thead>
              <tbody>
                {positions.map((p, i) => (
                  <tr key={i}>
                    <td style={{ fontWeight: 700 }}>{p.symbol ?? '—'}</td>
                    <td>
                      <span className={`badge ${(p.side ?? '').toLowerCase() === 'long' ? 'badge-green' : 'badge-red'}`}>
                        {p.side ?? '—'}
                      </span>
                    </td>
                    <td className="mono">{p.size  != null ? fmtUSD(p.size)  : '—'}</td>
                    <td className="mono dim">{p.entry != null ? fmt(p.entry, 4) : '—'}</td>
                    <td className="mono dim">{p.mark  != null ? fmt(p.mark,  4) : '—'}</td>
                    <td className={`mono ${clr(p.funding_earned)}`}>{fmtUSD(p.funding_earned)}</td>
                    <td className={`mono ${clr(p.unrealized_pnl)}`}>{fmtUSD(p.unrealized_pnl)}</td>
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
// TAB: LOGS
// API: { service, count, logs: [{raw: string, level: INFO|WARN|ERROR|DEBUG}] }
// ─────────────────────────────────────────────────────────────────────────────
const LOG_SERVICES = [
  { label: 'autonomous-trader', value: 'autonomous-trader' },
  { label: 'crypto_bsc',        value: 'crypto_bsc' },
  { label: 'ibc-gateway',       value: 'ibc-gateway' },
  { label: 'tgbot-ibkr',        value: 'tgbot-ibkr' },
  { label: 'tgbot-sniper',      value: 'tgbot-sniper' },
  { label: 'tgbot-grid',        value: 'tgbot-grid' },
  { label: 'tgbot-funding',     value: 'tgbot-funding' },
  { label: 'hermes-gateway',    value: 'hermes-gateway' },
];

function LogsTab() {
  const [service, setService] = useState('autonomous-trader');
  const [lines, setLines]     = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError]     = useState(null);
  const bottomRef = useRef(null);

  const fetchLogs = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await apiFetch(`/api/logs/${service}`);
      // logs is array of {raw, level} objects
      const raw = (data.logs ?? []).map((l) => (typeof l === 'object' ? l.raw : l) ?? '');
      setLines(raw);
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

  function parseLevel(line) {
    if (/ERROR|CRITICAL|FATAL/i.test(line)) return 'ERROR';
    if (/WARN/i.test(line))  return 'WARN';
    if (/DEBUG/i.test(line)) return 'DEBUG';
    return 'INFO';
  }

  return (
    <div className="card">
      <div className="card-title" style={{ justifyContent: 'space-between' }}>
        <span><FileText size={14} />Log Viewer</span>
        <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
          <select className="log-select" value={service} onChange={(e) => setService(e.target.value)}>
            {LOG_SERVICES.map((s) => (
              <option key={s.value} value={s.value}>{s.label}</option>
            ))}
          </select>
          <button className={`refresh-btn ${loading ? 'spinning' : ''}`} onClick={fetchLogs}>
            <RefreshCw size={12} />Refresh
          </button>
        </div>
      </div>

      {error ? <ErrBox msg={error} /> : (
        <div className="log-console">
          {lines.length === 0 && !loading && <span className="dim">Sem logs</span>}
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

// ─────────────────────────────────────────────────────────────────────────────
// TAB: SYSTEM
// API: { services: [{service, status, active, uptime_since}],
//        cpu_percent, load_avg, memory_used_pct, disk_used_pct (float),
//        disk_size, disk_used }
// ─────────────────────────────────────────────────────────────────────────────
function SystemTab({ data }) {
  if (!data) return <Loader />;

  const cpu    = data.cpu_percent     ?? null;
  const ram    = data.memory_used_pct ?? null;
  const disk   = typeof data.disk_used_pct === 'string'
    ? parseFloat(data.disk_used_pct)
    : data.disk_used_pct ?? null;

  const gauges = [
    { label: 'CPU',  icon: <Cpu size={14} />,         value: cpu,  extra: null },
    { label: 'RAM',  icon: <MemoryStick size={14} />,  value: ram,  extra: null },
    { label: 'Disk', icon: <HardDrive size={14} />,    value: disk, extra: `${data.disk_used ?? '?'} / ${data.disk_size ?? '?'}` },
  ].filter((g) => g.value != null);

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

  // services is always an array from the API
  const serviceList = Array.isArray(data.services)
    ? data.services
    : Object.entries(data.services ?? {}).map(([k, v]) => ({ service: k, status: v, active: v === 'active' || v === true }));

  return (
    <div>
      <div className="grid-3 section-gap">
        {gauges.map((g) => (
          <div key={g.label} className="card">
            <div className="card-title">{g.icon}{g.label}</div>
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginBottom: 10 }}>
              <span className="mono" style={{ fontSize: 28, fontWeight: 700, color: gaugeColor(g.value) }}>
                {fmt(g.value, 1)}%
              </span>
              {g.extra && <span className="dim mono" style={{ fontSize: 11 }}>{g.extra}</span>}
            </div>
            <div className="progress-bar">
              <div className={`progress-fill ${gaugeClass(g.value)}`} style={{ width: `${Math.min(g.value, 100)}%` }} />
            </div>
          </div>
        ))}
      </div>

      {data.load_avg && (
        <div className="card section-gap">
          <div className="card-title"><Activity size={14} />Load Average</div>
          <div style={{ display: 'flex', gap: 32 }}>
            {(Array.isArray(data.load_avg) ? data.load_avg : [data.load_avg]).map((v, i) => (
              <div key={i}>
                <div className="dim mono" style={{ fontSize: 10 }}>{['1m', '5m', '15m'][i] ?? `${i}`}</div>
                <div className="mono" style={{ fontSize: 22, color: 'var(--green)' }}>{fmt(Number(v), 2)}</div>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="card">
        <div className="card-title"><Server size={14} />Services</div>
        {serviceList.length === 0 ? (
          <div className="empty-state">Sem dados de serviços</div>
        ) : (
          serviceList.map((svc) => {
            const name   = svc.service ?? svc.name ?? '?';
            const isUp   = svc.active ?? (svc.status === 'active' || svc.status === 'running');
            const status = svc.status ?? (isUp ? 'active' : 'down');
            const since  = svc.uptime_since ?? '';
            return (
              <div key={name} className="service-item">
                <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                  {isUp
                    ? <CheckCircle size={14} style={{ color: 'var(--green)' }} />
                    : <XCircle    size={14} style={{ color: status === 'failed' ? 'var(--red)' : 'var(--yellow)' }} />}
                  <div>
                    <div className="service-name">{name}</div>
                    {since && <div className="dim mono" style={{ fontSize: 10 }}>{since}</div>}
                  </div>
                </div>
                <span className={`badge ${isUp ? 'badge-green' : status === 'failed' ? 'badge-red' : 'badge-yellow'}`}>
                  {status}
                </span>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// ROOT APP
// ─────────────────────────────────────────────────────────────────────────────
const TABS = [
  { id: 'overview', label: 'Overview',  icon: BarChart2  },
  { id: 'ibkr',     label: 'IBKR',      icon: TrendingUp },
  { id: 'sniper',   label: 'Sniper',    icon: Target     },
  { id: 'grid',     label: 'Grid',      icon: Grid       },
  { id: 'funding',  label: 'Funding',   icon: DollarSign },
  { id: 'logs',     label: 'Logs',      icon: FileText   },
  { id: 'system',   label: 'System',    icon: Server     },
];

export default function App() {
  const [activeTab, setActiveTab] = useState('overview');
  const [loading, setLoading]     = useState(false);
  const [online, setOnline]       = useState(null);
  const [lastUpdate, setLastUpdate] = useState(null);
  const [errors, setErrors]       = useState({});

  const [pnl,     setPnl]     = useState(null);
  const [ibkr,    setIbkr]    = useState(null);
  const [sniper,  setSniper]  = useState(null);
  const [grid,    setGrid]    = useState(null);
  const [funding, setFunding] = useState(null);
  const [system,  setSystem]  = useState(null);

  const fetchAll = useCallback(async () => {
    setLoading(true);
    const errs = {};
    const [pnlR, ibkrR, sniperR, gridR, fundingR, systemR] = await Promise.allSettled([
      apiFetch('/api/pnl'),
      apiFetch('/api/ibkr'),
      apiFetch('/api/sniper'),
      apiFetch('/api/grid'),
      apiFetch('/api/funding'),
      apiFetch('/api/system'),
    ]);
    if (pnlR.status     === 'fulfilled') setPnl(pnlR.value);       else errs.pnl = pnlR.reason?.message;
    if (ibkrR.status    === 'fulfilled') setIbkr(ibkrR.value);     else errs.ibkr = ibkrR.reason?.message;
    if (sniperR.status  === 'fulfilled') setSniper(sniperR.value); else errs.sniper = sniperR.reason?.message;
    if (gridR.status    === 'fulfilled') setGrid(gridR.value);     else errs.grid = gridR.reason?.message;
    if (fundingR.status === 'fulfilled') setFunding(fundingR.value); else errs.funding = fundingR.reason?.message;
    if (systemR.status  === 'fulfilled') setSystem(systemR.value); else errs.system = systemR.reason?.message;
    setErrors(errs);
    setOnline(Object.keys(errs).length < 6);
    setLastUpdate(new Date().toLocaleTimeString());
    setLoading(false);
  }, []);

  useEffect(() => {
    fetchAll();
    const id = setInterval(fetchAll, 30_000);
    return () => clearInterval(id);
  }, [fetchAll]);

  return (
    <div className="app">
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
            {online === null ? 'Connecting…' : online ? 'Online' : 'Parcial'}
          </div>
          <button className={`refresh-btn ${loading ? 'spinning' : ''}`} onClick={fetchAll}>
            <RefreshCw size={12} />Refresh
          </button>
        </div>
      </header>

      <nav className="tabs">
        {TABS.map((t) => {
          const Icon  = t.icon;
          const hasErr = errors[t.id];
          return (
            <button
              key={t.id}
              className={`tab ${activeTab === t.id ? 'active' : ''}`}
              onClick={() => setActiveTab(t.id)}
            >
              <Icon size={14} />
              {t.label}
              {hasErr && <span className="tab-badge" style={{ background: 'var(--red)' }}>!</span>}
            </button>
          );
        })}
      </nav>

      <main className="content">
        {activeTab === 'overview' && <OverviewTab pnl={pnl} ibkr={ibkr} sniper={sniper} grid={grid} />}
        {activeTab === 'ibkr'     && (errors.ibkr    ? <ErrBox msg={errors.ibkr}    /> : <IBKRTab    data={ibkr}    />)}
        {activeTab === 'sniper'   && (errors.sniper  ? <ErrBox msg={errors.sniper}  /> : <SniperTab  data={sniper}  />)}
        {activeTab === 'grid'     && (errors.grid    ? <ErrBox msg={errors.grid}    /> : <GridTab    data={grid}    />)}
        {activeTab === 'funding'  && (errors.funding ? <ErrBox msg={errors.funding} /> : <FundingTab data={funding} />)}
        {activeTab === 'logs'     && <LogsTab />}
        {activeTab === 'system'   && (errors.system  ? <ErrBox msg={errors.system}  /> : <SystemTab  data={system}  />)}
      </main>
    </div>
  );
}
