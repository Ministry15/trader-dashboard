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
  BarChart2, Target, X, Download, Gauge, Search
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
function OverviewTab({ pnl, ibkr, sniper, grid, flashArb, system, liquidationsAll, gas, errors }) {
  if (!pnl) return <Loader />;

  // ─ P&L ─
  const todayBNB    = pnl.today?.sniper_bnb   ?? 0;
  const todayUSDT   = pnl.today?.grid_usdt    ?? 0;
  const totalTrades = (pnl.today?.sniper_trades ?? 0) + (pnl.today?.grid_trades ?? 0);
  const winRate     = pnl.today?.sniper_win_rate ?? sniper?.win_rate ?? 0;
  const history     = pnl.history_7d ?? [];
  const chartData   = history.map(d => ({
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

  // ─ Liquidations aggregate ─
  const LIQ_IDS = ['base','polygon','avax','arb','op','scroll','linea',
    'compound_base','compound_polygon','compound_arb','compound_op',
    'morpho_base','morpho_polygon','morpho_arb'];
  const liqTotalProfit = LIQ_IDS.reduce((s, id) =>
    s + (Number(liquidationsAll?.[id]?.summary?.total_est_profit) || 0), 0);
  const liqExecutedProfit = LIQ_IDS.reduce((s, id) =>
    s + (Number(liquidationsAll?.[id]?.summary?.executed_profit) || 0), 0);
  const liqBestOpp = LIQ_IDS.reduce((best, id) => {
    const v = Number(liquidationsAll?.[id]?.summary?.best_profit) || 0;
    return v > best ? v : best;
  }, 0);
  const liqWatching   = LIQ_IDS.reduce((cnt, id) =>
    cnt + (liquidationsAll?.[id]?.opportunities ?? []).filter(o => o.health_factor >= 1.0 && o.health_factor < 1.2).length, 0);
  const liqLiquidable = LIQ_IDS.reduce((cnt, id) =>
    cnt + (liquidationsAll?.[id]?.opportunities ?? []).filter(o => o.health_factor < 1.0).length, 0);

  // ─ Gas per chain ─
  const GAS_CHAINS = [
    { id: 'base',    label: 'BASE',      symbol: 'ETH',  warn: 0.005 },
    { id: 'arb',     label: 'ARBITRUM',  symbol: 'ETH',  warn: 0.003 },
    { id: 'op',      label: 'OPTIMISM',  symbol: 'ETH',  warn: 0.003 },
    { id: 'scroll',  label: 'SCROLL',    symbol: 'ETH',  warn: 0.002 },
    { id: 'linea',   label: 'LINEA',     symbol: 'ETH',  warn: 0.002 },
    { id: 'polygon', label: 'POLYGON',   symbol: 'POL',  warn: 10    },
    { id: 'avax',    label: 'AVALANCHE', symbol: 'AVAX', warn: 0.05  },
  ];

  // ─ Real profit per bot ─
  const PROFIT_BOTS = [
    { id: 'base',             label: 'AAVE BASE'     },
    { id: 'polygon',          label: 'AAVE POLYGON'  },
    { id: 'avax',             label: 'AAVE AVAX'     },
    { id: 'arb',              label: 'AAVE ARB'      },
    { id: 'op',               label: 'AAVE OP'       },
    { id: 'scroll',           label: 'AAVE SCROLL'   },
    { id: 'linea',            label: 'AAVE LINEA'    },
    { id: 'compound_base',    label: 'CMPD BASE'     },
    { id: 'compound_polygon', label: 'CMPD POLYGON'  },
    { id: 'compound_arb',     label: 'CMPD ARB'      },
    { id: 'compound_op',      label: 'CMPD OP'       },
    { id: 'morpho_base',      label: 'MORPHO BASE'   },
    { id: 'morpho_polygon',   label: 'MORPHO POLYGON'},
    { id: 'morpho_arb',       label: 'MORPHO ARB'    },
  ];

  // ─ Bot status ─
  const serviceList = system
    ? (Array.isArray(system.services)
        ? system.services
        : Object.entries(system.services ?? {}).map(([k, v]) => ({
            service: k,
            status: typeof v === 'string' ? v : (v ? 'active' : 'down'),
            active: v === 'active' || v === true,
          })))
    : [];
  const botsActive = serviceList.filter(s =>
    s.active ?? (s.status === 'active' || s.status === 'running')).length;

  // ─ Alerts ─
  const ALERT_LABELS = {
    pnl:          'P&L sem dados',
    ibkr:         'IBKR sem resposta',
    sniper:       'Sniper bot sem resposta',
    grid:         'Grid bot sem resposta',
    funding:      'Funding sem dados',
    flashArb:     'Flash Arb sem dados',
    liquidations: 'Liquidações (Base) sem dados',
    system:       'System monitor sem resposta',
  };
  const alerts = Object.entries(errors ?? {})
    .filter(([, msg]) => msg)
    .map(([key, msg]) => ({ label: ALERT_LABELS[key] ?? key, msg }));

  return (
    <div>
      {/* ── Alerts ─────────────────────────────────────────────────────────── */}
      {alerts.length > 0 && (
        <div style={{ marginBottom: '1rem' }}>
          {alerts.map((a, i) => (
            <div key={i} style={{
              display: 'flex', alignItems: 'center', gap: '0.6rem',
              background: '#1a0a00', border: '1px solid #5a2a00',
              borderLeft: '3px solid var(--yellow)',
              borderRadius: 4, padding: '0.45rem 0.75rem',
              marginBottom: '0.4rem', fontSize: '0.85em',
            }}>
              <AlertTriangle size={13} style={{ color: 'var(--yellow)', flexShrink: 0 }} />
              <span style={{ color: 'var(--yellow)', fontWeight: 700 }}>{a.label}</span>
              <span style={{ color: '#888' }}>{a.msg}</span>
            </div>
          ))}
        </div>
      )}

      {/* ── Top KPIs ────────────────────────────────────────────────────────── */}
      <div className="kpi-grid mb">
        <div className="kpi">
          <div className="kpi-label">// BOTS ACTIVOS</div>
          <div className="kpi-val neu">
            {system ? `${botsActive}/${serviceList.length}` : '—'}
          </div>
          <div className="kpi-sub">SYSTEM SERVICES</div>
        </div>
        <div className="kpi">
          <div className="kpi-label">// LIQ. ESTIMADO HOJE</div>
          <div className={`kpi-val ${clr(liqTotalProfit)}`}>{fmtUSD(liqTotalProfit)}</div>
          <div className="kpi-sub">TODAS AS CHAINS</div>
        </div>
        <div className="kpi">
          <div className="kpi-label">// MELHOR OPP. HOJE</div>
          <div className={`kpi-val ${clr(liqBestOpp)}`}>{fmtUSD(liqBestOpp)}</div>
          <div className="kpi-sub">LIQUIDAÇÕES</div>
        </div>
        <div className="kpi">
          <div className="kpi-label">// EM VIGILÂNCIA</div>
          <div className="kpi-val neu">{liqWatching}</div>
          <div className="kpi-sub">
            {liqLiquidable > 0 ? `+${liqLiquidable} LIQUIDÁVEIS` : 'HF 1.0–1.2'}
          </div>
        </div>
      </div>

      {/* ── Bot Status + P&L breakdown ──────────────────────────────────────── */}
      <div className="row2 mb">
        <div className="panel">
          <div className="panel-head"><CheckCircle size={11} />&nbsp;STATUS DOS BOTS</div>
          {serviceList.length === 0 ? (
            <div className="empty">SEM DADOS DE SISTEMA</div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
              {serviceList.map(svc => {
                const name   = svc.service ?? svc.name ?? '?';
                const isUp   = svc.active ?? (svc.status === 'active' || svc.status === 'running');
                const status = svc.status ?? (isUp ? 'active' : 'down');
                return (
                  <div key={name} className="svc-row">
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                      {isUp
                        ? <CheckCircle size={12} style={{ color: 'var(--green)', flexShrink: 0 }} />
                        : <XCircle    size={12} style={{ color: status === 'failed' ? 'var(--red)' : 'var(--yellow)', flexShrink: 0 }} />}
                      <span className="svc-name">{name}</span>
                    </div>
                    <span className={`badge ${isUp ? 'bg' : status === 'failed' ? 'br' : 'by'}`}>{status}</span>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        <div className="panel">
          <div className="panel-head">P&amp;L BREAKDOWN — HOJE</div>
          <div className="kv-list">
            <div className="kv">
              <span className="kv-k">SNIPER</span>
              <span className={clr(todayBNB)}>{fmtBNB(todayBNB)}</span>
            </div>
            <div className="kv">
              <span className="kv-k">GRID</span>
              <span className={clr(todayUSDT)}>{fmtUSD(todayUSDT)}</span>
            </div>
            {flashArb != null && (
              <div className="kv">
                <span className="kv-k">FLASH ARB</span>
                <span className={clr(flashArb.total_pnl_usd ?? 0)}>{fmtUSD(flashArb.total_pnl_usd ?? 0)}</span>
              </div>
            )}
            <div className="kv">
              <span className="kv-k">LIQ. ESTIMADO</span>
              <span className={clr(liqTotalProfit)}>{fmtUSD(liqTotalProfit)}</span>
            </div>
            <div className="kv">
              <span className="kv-k">LIQ. REAL</span>
              <span className={clr(liqExecutedProfit)}>{fmtUSD(liqExecutedProfit)}</span>
            </div>
            <div className="kv" style={{ borderTop: '1px solid #222', paddingTop: 8, marginTop: 2 }}>
              <span className="kv-k">WIN RATE</span>
              <span>{pct(winRate)}</span>
            </div>
            <div className="kv">
              <span className="kv-k">TRADES</span>
              <span className="neu">{totalTrades}</span>
            </div>
            <div className="kv">
              <span className="kv-k">REGIME</span>
              <span className={`regime-tag regime-${regimeClass}`}>{regime}</span>
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
          </div>
        </div>
      </div>

      {/* ── Gas por chain + Lucro real ─────────────────────────────────────── */}
      <div className="row2 mb">
        <div className="panel">
          <div className="panel-head"><Zap size={11} />&nbsp;GAS POR CHAIN</div>
          {!gas ? (
            <div className="empty">A carregar…</div>
          ) : (
            <div className="kv-list">
              {GAS_CHAINS.map(({ id, label, symbol, warn }) => {
                const b = gas[id];
                const bal = b?.balance;
                const isLow = bal != null && bal < warn;
                const isNull = bal == null;
                return (
                  <div key={id} className="kv">
                    <span className="kv-k">{label}</span>
                    <span style={{ color: isNull ? '#555' : isLow ? 'var(--yellow)' : 'var(--green)' }}>
                      {isNull ? '—' : `${bal.toFixed(4)} ${symbol}`}
                    </span>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        <div className="panel">
          <div className="panel-head"><DollarSign size={11} />&nbsp;LUCRO REAL POR BOT</div>
          <div className="kv-list">
            {PROFIT_BOTS.map(({ id, label }) => {
              const prof = Number(liquidationsAll?.[id]?.summary?.executed_profit) || 0;
              if (prof <= 0) return null;
              return (
                <div key={id} className="kv">
                  <span className="kv-k">{label}</span>
                  <span className="pos">{fmtUSD(prof)}</span>
                </div>
              );
            })}
            <div className="kv" style={{ borderTop: '1px solid #222', paddingTop: 8, marginTop: 2 }}>
              <span className="kv-k">TOTAL EXECUTADO</span>
              <span className={clr(liqExecutedProfit)}>{fmtUSD(liqExecutedProfit)}</span>
            </div>
          </div>
        </div>
      </div>

      {/* ── 7-day chart + Weekly summary ────────────────────────────────────── */}
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
              <span className="kv-k">SNIPER 7D</span>
              <span className={clr(weekSniper)}>{fmtBNB(weekSniper)}</span>
            </div>
            <div className="kv">
              <span className="kv-k">GRID 7D</span>
              <span className={clr(weekGrid)}>{fmtUSD(weekGrid)}</span>
            </div>
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
            {flashArb?.service_status != null && (
              <div className="kv">
                <span className="kv-k">FLASH ARB BOT</span>
                <span className={`badge ${flashArb.service_status.active ? 'bg' : 'br'}`} style={{ fontSize: 9 }}>
                  {flashArb.service_status.status ?? 'unknown'}
                </span>
              </div>
            )}
            <div className="kv">
              <span className="kv-k">LIQ. WATCHING</span>
              <span style={{ color: liqWatching > 0 ? 'var(--yellow)' : 'var(--text-sec)' }}>{liqWatching}</span>
            </div>
            {liqLiquidable > 0 && (
              <div className="kv">
                <span className="kv-k">LIQ. NOW</span>
                <span style={{ color: 'var(--red)' }}>{liqLiquidable}</span>
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
function LiquidationsPanel({ data, defaultMinProfit = 25 }) {
  if (!data) return <Loader />;
  const opps    = data.opportunities ?? [];
  const summary = data.summary       ?? {};
  const getProfit = o => Number(o.estimated_profit_usd ?? o.estimated_profit) || 0;
  const totalEstProfit = opps.reduce((s, o) => s + getProfit(o), 0);
  const bestProfit     = opps.reduce((m, o) => Math.max(m, getProfit(o)), 0);

  const [minProfit,  setMinProfit]  = useState(String(defaultMinProfit));
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
                        .filter(o => getProfit(o) >= (Number(minProfit) || 0))
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
        {fmtUSD(getProfit(o))}
      </td>
    </tr>
  );

  return (
    <div>
      <div className="kpi-row">
        <div className="kpi-card">
          <div className="kpi-label">Oportunidades</div>
          <div className="kpi-value">{opps.length}</div>
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
          <div className={`kpi-value ${clr(bestProfit)}`}>
            {fmtUSD(bestProfit)}
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
              onBlur={e => { if (e.target.value.trim() === '') setMinProfit(String(defaultMinProfit)); }}
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

// ─── RESUMO PANEL ─────────────────────────────────────────────────────────────
function ResumoPanel({ allData }) {
  const CHAIN_META = [
    { id: 'base',             label: 'Base',      protocol: 'Aave V3'     },
    { id: 'polygon',          label: 'Polygon',   protocol: 'Aave V3'     },
    { id: 'avax',             label: 'Avalanche', protocol: 'Aave V3'     },
    { id: 'arb',              label: 'Arbitrum',  protocol: 'Aave V3'     },
    { id: 'op',               label: 'Optimism',  protocol: 'Aave V3'     },
    { id: 'scroll',           label: 'Scroll',    protocol: 'Aave V3'     },
    { id: 'linea',            label: 'Linea',     protocol: 'Aave V3'     },
    { id: 'compound_base',    label: 'Base',      protocol: 'Compound V3' },
    { id: 'compound_polygon', label: 'Polygon',   protocol: 'Compound V3' },
    { id: 'compound_arb',     label: 'Arbitrum',  protocol: 'Compound V3' },
    { id: 'compound_op',      label: 'Optimism',  protocol: 'Compound V3' },
    { id: 'morpho_base',      label: 'Base',      protocol: 'Morpho Blue' },
    { id: 'morpho_polygon',   label: 'Polygon',   protocol: 'Morpho Blue' },
    { id: 'morpho_arb',       label: 'Arbitrum',  protocol: 'Morpho Blue' },
  ];

  const rows = CHAIN_META.map(({ id, label, protocol }) => {
    const d = allData[id];
    if (!d) return { id, label, protocol, opp: 0, avgProfit: null, maxProfit: null, hasData: false };
    const s = d.summary ?? {};
    const opps = d.opportunities ?? [];
    const profits = opps.map(o => Number(o.estimated_profit_usd ?? o.estimated_profit) || 0).filter(v => v > 0);
    const avgProfit = profits.length ? profits.reduce((a, b) => a + b, 0) / profits.length : 0;
    const maxProfit = profits.length ? Math.max(...profits) : 0;
    return { id, label, protocol, opp: opps.length, avgProfit, maxProfit, hasData: true };
  });

  const totalOpp    = rows.reduce((s, r) => s + (r.opp || 0), 0);
  const totalProfit = CHAIN_META.reduce((s, { id }) => s + (allData[id]?.opportunities ?? []).reduce((a, o) => a + (Number(o.estimated_profit_usd ?? o.estimated_profit) || 0), 0), 0);
  const bestOpp     = rows.reduce((best, r) => (r.maxProfit ?? 0) > best ? (r.maxProfit ?? 0) : best, 0);

  return (
    <div>
      <div className="kpi-row">
        <div className="kpi-card">
          <div className="kpi-label">Total Oportunidades</div>
          <div className="kpi-value">{totalOpp}</div>
        </div>
        <div className="kpi-card">
          <div className="kpi-label">Lucro Total Estimado</div>
          <div className={`kpi-value ${clr(totalProfit)}`}>{fmtUSD(totalProfit)}</div>
        </div>
        <div className="kpi-card">
          <div className="kpi-label">Melhor Oportunidade</div>
          <div className={`kpi-value ${clr(bestOpp)}`}>{fmtUSD(bestOpp)}</div>
        </div>
      </div>

      <div style={{ overflowX: 'auto', marginTop: '1.25rem' }}>
        <table>
          <thead>
            <tr>
              <th>Chain</th>
              <th>Protocolo</th>
              <th>Oportunidades</th>
              <th>Lucro Médio</th>
              <th>Lucro Máximo</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(r => (
              <tr key={r.id} style={!r.hasData ? { opacity: 0.45 } : {}}>
                <td>{r.label}</td>
                <td style={{ color: '#aaa' }}>{r.protocol}</td>
                <td>{r.hasData ? r.opp : '—'}</td>
                <td className={r.hasData ? clr(r.avgProfit) : ''}>{r.hasData ? fmtUSD(r.avgProfit) : '—'}</td>
                <td className={r.hasData ? clr(r.maxProfit) : ''}>{r.hasData ? fmtUSD(r.maxProfit) : '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ─── LIQUIDATIONS TAB ─────────────────────────────────────────────────────────
function LiquidationsTab({ dataBase, dataPolygon, dataAvax, dataArb, dataOp, dataScroll, dataLinea, dataCompoundBase, dataMorphoBase, dataCompoundPolygon, dataCompoundArb, dataCompoundOp, dataMorphoPolygon, dataMorphoArb }) {
  const [l1Tab,         setL1Tab]         = useState('resumo');
  const [aaveChain,     setAaveChain]     = useState('base');
  const [compoundChain, setCompoundChain] = useState('compound_base');
  const [morphoChain,   setMorphoChain]   = useState('morpho_base');

  const allData = {
    base: dataBase, polygon: dataPolygon, avax: dataAvax, arb: dataArb,
    op: dataOp, scroll: dataScroll, linea: dataLinea,
    compound_base: dataCompoundBase, compound_polygon: dataCompoundPolygon,
    compound_arb: dataCompoundArb, compound_op: dataCompoundOp,
    morpho_base: dataMorphoBase, morpho_polygon: dataMorphoPolygon, morpho_arb: dataMorphoArb,
  };

  const L1_TABS = [
    { id: 'resumo',   label: 'Resumo',      color: '#e8b800' },
    { id: 'aave',     label: 'Aave V3',     color: '#B6509E' },
    { id: 'compound', label: 'Compound V3', color: '#00D395' },
    { id: 'morpho',   label: 'Morpho Blue', color: '#2470FF' },
  ];

  const AAVE_CHAINS = [
    { id: 'base',    label: 'Base',      color: '#2d6ae0' },
    { id: 'polygon', label: 'Polygon',   color: '#8247e5' },
    { id: 'avax',    label: 'Avalanche', color: '#e84142' },
    { id: 'arb',     label: 'Arbitrum',  color: '#28A0F0' },
    { id: 'op',      label: 'Optimism',  color: '#FF0420' },
    { id: 'scroll',  label: 'Scroll',    color: '#FFDBB0' },
    { id: 'linea',   label: 'Linea',     color: '#61DFFF' },
  ];

  const COMPOUND_CHAINS = [
    { id: 'compound_base',    label: 'Base',     color: '#00D395' },
    { id: 'compound_polygon', label: 'Polygon',  color: '#00A86B' },
    { id: 'compound_arb',     label: 'Arbitrum', color: '#00B4D8' },
    { id: 'compound_op',      label: 'Optimism', color: '#E8533F' },
  ];

  const MORPHO_CHAINS = [
    { id: 'morpho_base',    label: 'Base',     color: '#2470FF' },
    { id: 'morpho_polygon', label: 'Polygon',  color: '#9B59B6' },
    { id: 'morpho_arb',     label: 'Arbitrum', color: '#1A9FFF' },
  ];

  const DESCRIPTIONS = {
    base:             'Aave V3 — protocolo de lending líder. Liquidas posições subcapitalizadas e recebes 5–7% de bonus.',
    polygon:          'Aave V3 na Polygon — gas ultra barato (~$0.01). Mesmo mecanismo do Base com menos competição.',
    avax:             'Aave V3 na Avalanche — gas barato, mercado menos competitivo que Ethereum.',
    arb:              'Aave V3 no Arbitrum — L2 com alto volume. Mais oportunidades mas mais competição.',
    op:               'Aave V3 no Optimism — L2 similar ao Base. Gas barato, bom volume de posições.',
    scroll:           'Aave V3 no Scroll — L2 nova com poucos bots competidores. Menor volume mas maior facilidade de captura.',
    linea:            'Aave V3 no Linea — L2 nova da Consensys. Actividade crescente, competição baixa.',
    compound_base:    'Compound V3 (Comet) na Base — protocolo alternativo ao Aave. Bonus de liquidação 8–10%, maior que o Aave.',
    compound_polygon: 'Compound V3 (Comet) na Polygon — dois mercados: USDC + USDT. Bonus 8%, gas em MATIC (~$0.01), menos competição que Base.',
    compound_arb:     'Compound V3 (Comet) no Arbitrum — dois mercados: USDC + USDT. Bonus 8%, gas em ETH. Alto volume de posições.',
    compound_op:      'Compound V3 (Comet) no Optimism — dois mercados: USDC + USDT. Bonus 8%, gas em ETH (~$0.001). L2 com boa actividade.',
    morpho_base:      'Morpho Blue na Base — mercados isolados com LIF até 15%. 5000+ posições, o maior volume de todos os protocolos.',
    morpho_polygon:   'Morpho Blue na Polygon — mercados isolados com LIF variável. Gas em MATIC (~$0.01). Protocolo novo, competição baixa.',
    morpho_arb:       'Morpho Blue no Arbitrum — mercados isolados com LIF variável. Gas em ETH. Alto volume, mesmo protocolo que a Base.',
  };

  const l1BtnStyle = (id, color) => ({
    padding: '0.45rem 1.4rem',
    borderRadius: 4,
    border: l1Tab === id ? `2px solid ${color}` : '2px solid transparent',
    cursor: 'pointer',
    fontSize: '0.92em',
    fontWeight: l1Tab === id ? 700 : 400,
    background: l1Tab === id ? color : '#2a2a2a',
    color: l1Tab === id ? (id === 'resumo' ? '#111' : '#fff') : '#ccc',
    transition: 'background 0.15s',
  });

  const chipStyle = (id, activeId, color) => ({
    padding: '0.28rem 0.9rem',
    borderRadius: 4,
    border: activeId === id ? `2px solid ${color}` : '2px solid #333',
    cursor: 'pointer',
    fontSize: '0.82em',
    fontWeight: activeId === id ? 700 : 400,
    background: activeId === id ? color : '#1a1a1a',
    color: '#fff',
    transition: 'background 0.15s',
  });

  let activeChainId   = null;
  let activeChains    = null;
  let setActiveChain  = null;
  if (l1Tab === 'aave') {
    activeChainId  = aaveChain;
    activeChains   = AAVE_CHAINS;
    setActiveChain = setAaveChain;
  } else if (l1Tab === 'compound') {
    activeChainId  = compoundChain;
    activeChains   = COMPOUND_CHAINS;
    setActiveChain = setCompoundChain;
  } else if (l1Tab === 'morpho') {
    activeChainId  = morphoChain;
    activeChains   = MORPHO_CHAINS;
    setActiveChain = setMorphoChain;
  }

  const activeColor = activeChains?.find(c => c.id === activeChainId)?.color ?? '#444';

  return (
    <div>
      <div style={{
        position: 'sticky', top: 0, zIndex: 100,
        background: '#0a0a0a',
        borderBottom: '1px solid #333',
        marginTop: '-18px',
        paddingTop: 'calc(18px + 0.5rem)',
        paddingBottom: '0.75rem',
        marginBottom: '1rem',
      }}>
        {/* Level 1 tabs */}
        <div style={{ display: 'flex', gap: '0.5rem', marginBottom: activeChains ? '0.65rem' : 0 }}>
          {L1_TABS.map(({ id, label, color }) => (
            <button key={id} onClick={() => setL1Tab(id)} style={l1BtnStyle(id, color)}>
              {label}
            </button>
          ))}
        </div>

        {/* Level 2 chain chips */}
        {activeChains && (
          <div style={{ display: 'flex', gap: '0.4rem', flexWrap: 'wrap' }}>
            {activeChains.map(({ id, label, color }) => (
              <button key={id} onClick={() => setActiveChain(id)} style={chipStyle(id, activeChainId, color)}>
                {label}
              </button>
            ))}
          </div>
        )}
      </div>

      {l1Tab === 'resumo' && <ResumoPanel allData={allData} />}

      {l1Tab !== 'resumo' && activeChainId && (() => {
        const MIN_PROFIT_DEFAULTS = {
          base:             8,
          polygon:          5,
          avax:             10,
          arb:              8,
          op:               8,
          scroll:           5,
          linea:            5,
          compound_base:    8,
          compound_polygon: 5,
          compound_arb:     8,
          compound_op:      8,
          morpho_base:      8,
          morpho_polygon:   5,
          morpho_arb:       8,
        };
        const data = allData[activeChainId];
        const desc = DESCRIPTIONS[activeChainId];
        const chainLabel = activeChains?.find(c => c.id === activeChainId)?.label ?? activeChainId;
        const defaultMin = MIN_PROFIT_DEFAULTS[activeChainId] ?? 25;
        return (
          <>
            {desc && (
              <div style={{
                fontSize: '0.82em', color: '#888', marginBottom: '1.1rem',
                padding: '0.5rem 0.75rem', background: '#151515',
                borderRadius: 4, borderLeft: `3px solid ${activeColor}`,
              }}>
                {desc}
              </div>
            )}
            {!data
              ? <div style={{ color: '#888', padding: '2rem 0', textAlign: 'center' }}>
                  Sem dados {chainLabel} ainda
                </div>
              : <LiquidationsPanel key={activeChainId} data={data} defaultMinProfit={defaultMin} />
            }
          </>
        );
      })()}
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

// ─── SPEED ────────────────────────────────────────────────────────────────────
// ─── Oportunidades Tab ────────────────────────────────────────────────────────
function OpportunitiesTab({ data, onFetch, loading, window: win, onWindow }) {
  const events  = data?.events  ?? [];
  const summary = data?.summary ?? {};

  const CHAIN_COLOR = { Base: '#2c7be5', Arbitrum: '#28a8e0', Optimism: '#ff0420', Polygon: '#8247e5' };
  const PROTO_COLOR = { 'Aave V3': '#b6509e', 'Morpho Blue': '#1a4de3', 'Compound V3': '#00d395' };

  function capAddr(a) {
    if (!a || a.length < 10) return a;
    return a.slice(0, 6) + '…' + a.slice(-4);
  }
  function explorerUrl(chain, tx) {
    const base = { Base: 'https://basescan.org', Arbitrum: 'https://arbiscan.io',
                   Optimism: 'https://optimistic.ethscan.io', Polygon: 'https://polygonscan.com' };
    return `${base[chain] ?? 'https://etherscan.io'}/tx/${tx}`;
  }
  function fmtProfit(p) {
    if (p == null) return '?';
    return '$' + p.toFixed(2);
  }

  const pLost = summary.profit_lost ?? 0;
  const pCap  = summary.profit_captured ?? 0;
  const pTot  = pLost + pCap;
  const captureRate = pTot > 0 ? Math.round(pCap / pTot * 100) : 0;

  return (
    <div>
      {/* Summary bar */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 10, marginBottom: 14 }}>
        {[
          { label: 'Total Liquidações', val: summary.total ?? '—', color: 'var(--accent)' },
          { label: 'Capturado por Nós', val: summary.by_us != null ? `${summary.by_us}  ($${pCap.toFixed(2)})` : '—', color: 'var(--green)' },
          { label: 'Perdido Competitors', val: summary.by_competitor != null ? `${summary.by_competitor}  ($${pLost.toFixed(2)})` : '—', color: 'var(--red)' },
          { label: 'Taxa de Captura', val: data ? `${captureRate}%` : '—', color: captureRate > 30 ? 'var(--green)' : captureRate > 10 ? 'var(--yellow)' : 'var(--red)' },
        ].map(({ label, val, color }) => (
          <div key={label} className="panel" style={{ padding: '12px 14px' }}>
            <div style={{ fontSize: 11, color: '#888', marginBottom: 4 }}>{label}</div>
            <div style={{ fontSize: 15, fontWeight: 700, color }}>{val}</div>
          </div>
        ))}
      </div>

      {/* Controls */}
      <div className="panel mb">
        <div className="panel-head" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <span>LIQUIDAÇÕES ON-CHAIN — {data ? `${events.length} EVENTOS` : 'NÃO CARREGADO'}</span>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            {['1d', '2d', '7d'].map(w => (
              <button key={w}
                className="btn"
                style={{ padding: '3px 10px', opacity: win === w ? 1 : 0.5, fontWeight: win === w ? 700 : 400 }}
                onClick={() => onWindow(w)}>
                {w === '1d' ? 'Hoje' : w === '2d' ? 'Ontem' : '7 dias'}
              </button>
            ))}
            <button
              className={`btn${loading ? ' spin' : ''}`}
              onClick={onFetch}
              disabled={loading}
              style={{ marginLeft: 4 }}
            >
              {loading ? <><RefreshCw size={11} /> carregando...</> : <><Search size={11} /> PESQUISAR</>}
            </button>
          </div>
        </div>

        {!data && !loading && (
          <div style={{ padding: '30px', textAlign: 'center', color: '#666', fontSize: 12 }}>
            Clique em PESQUISAR para carregar liquidações on-chain nas últimas {win === '1d' ? '24h' : win === '2d' ? '48h' : '7 dias'}.
          </div>
        )}

        {loading && (
          <div style={{ padding: '30px', textAlign: 'center', color: '#888', fontSize: 12 }}>
            <RefreshCw size={14} style={{ animation: 'spin 1s linear infinite' }} /> A consultar {13} contratos em {4} chains...
          </div>
        )}

        {data && !loading && events.length === 0 && (
          <div style={{ padding: '20px', textAlign: 'center', color: '#666', fontSize: 12 }}>
            Nenhuma liquidação encontrada no período seleccionado.
          </div>
        )}

        {data && events.length > 0 && (
          <div style={{ overflowX: 'auto' }}>
            <table className="data-table" style={{ fontSize: 11 }}>
              <thead>
                <tr>
                  <th>Status</th>
                  <th>Protocolo</th>
                  <th>Chain</th>
                  <th>Dívida</th>
                  <th>Lucro Est.</th>
                  <th>Liquidador</th>
                  <th>TX</th>
                </tr>
              </thead>
              <tbody>
                {events.map((e, i) => {
                  const won = e.by_us;
                  const rowStyle = { borderLeft: `3px solid ${won ? 'var(--green)' : 'var(--red)'}` };
                  return (
                    <tr key={i} style={rowStyle}>
                      <td>
                        <span style={{
                          display: 'inline-block', padding: '2px 7px', borderRadius: 3, fontSize: 10,
                          fontWeight: 700, letterSpacing: 0.5,
                          background: won ? 'rgba(0,200,80,0.15)' : 'rgba(220,50,50,0.15)',
                          color: won ? 'var(--green)' : 'var(--red)',
                        }}>
                          {won ? '✓ GANHÁMOS' : '✗ PERDEMOS'}
                        </span>
                      </td>
                      <td>
                        <span style={{ color: PROTO_COLOR[e.protocol] ?? '#aaa', fontWeight: 600 }}>
                          {e.protocol}
                        </span>
                      </td>
                      <td>
                        <span style={{ color: CHAIN_COLOR[e.chain] ?? '#aaa' }}>{e.chain}</span>
                      </td>
                      <td>
                        <span style={{ color: '#ddd' }}>
                          {e.debt_usd != null ? `$${e.debt_usd.toFixed(2)}` : '—'}
                        </span>
                        {' '}
                        <span style={{ color: '#666', fontSize: 10 }}>{e.debt_asset}</span>
                      </td>
                      <td style={{ color: won ? 'var(--green)' : 'var(--red)', fontWeight: 600 }}>
                        {fmtProfit(e.profit_est)}
                      </td>
                      <td style={{ fontFamily: 'monospace', color: won ? 'var(--green)' : '#aaa' }}>
                        {capAddr(e.liquidator)}
                      </td>
                      <td>
                        <a href={explorerUrl(e.chain, e.tx_hash)} target="_blank" rel="noreferrer"
                           style={{ color: 'var(--accent)', textDecoration: 'none', fontFamily: 'monospace', fontSize: 10 }}>
                          {e.tx_hash ? e.tx_hash.slice(0, 10) + '…' : '—'}
                        </a>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}

        {data && (
          <div style={{ padding: '8px 14px', borderTop: '1px solid #222', fontSize: 10, color: '#555' }}>
            Protocolos: Aave V3 (Base/Arb/Op/Polygon) · Morpho Blue (Base/Arb) · Compound V3 (Base/Arb/Op/Polygon)
            {' · '}ETH: ${(data.eth_price ?? 0).toFixed(0)}
            {' · '}
            {data.timestamp ? new Date(data.timestamp + 'Z').toLocaleTimeString() : ''}
          </div>
        )}
      </div>
    </div>
  );
}

function SpeedTab({ data, onMeasure, loading }) {
  function latColor(v) {
    if (v == null) return '#555';
    if (v < 50)   return 'var(--green)';
    if (v < 150)  return 'var(--yellow)';
    return 'var(--red)';
  }
  function deltaColor(v) {
    if (v == null) return '#555';
    return v > 50 ? 'var(--red)' : v > 0 ? 'var(--yellow)' : 'var(--green)';
  }
  function fmtDelta(v) {
    if (v == null) return '—';
    return (v > 0 ? '+' : '') + v + 'ms';
  }

  const bots = data?.bots ?? [];

  return (
    <div>
      <div className="panel mb">
        <div className="panel-head" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <span>RPC SPEED — {bots.length > 0 ? `${bots.length} BOTS` : 'NÃO MEDIDO'}</span>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            {bots.length > 0 && (
              <span style={{ fontSize: 10, fontWeight: 400 }}>
                <span style={{ color: 'var(--green)' }}>● &lt;50ms</span>
                {' · '}
                <span style={{ color: 'var(--yellow)' }}>● 50-150ms</span>
                {' · '}
                <span style={{ color: 'var(--red)' }}>● &gt;150ms</span>
              </span>
            )}
            <button
              className={`btn${loading ? ' spin' : ''}`}
              onClick={onMeasure}
              disabled={loading}
              style={{ padding: '2px 8px', fontSize: 10 }}
            >
              <RefreshCw size={9} />
              {loading ? ' A MEDIR...' : ' MEDIR AGORA'}
            </button>
          </div>
        </div>

        {loading && bots.length === 0 && (
          <div style={{ textAlign: 'center', padding: '32px 0', color: '#666', fontSize: 11 }}>
            A medir latência RPC para {'{'}17{'}'} bots em paralelo… (~10s)
          </div>
        )}

        {!loading && bots.length === 0 && (
          <div className="empty" style={{ padding: '32px 0' }}>
            Clica "MEDIR AGORA" para medir a velocidade real de todos os bots
          </div>
        )}

        {bots.length > 0 && (
          <>
            <div style={{ overflowX: 'auto', marginTop: 10 }}>
              <table style={{ width: '100%', fontSize: 10, borderCollapse: 'collapse' }}>
                <thead>
                  <tr style={{ color: '#555', borderBottom: '1px solid #2a2a2a' }}>
                    <th style={{ padding: '4px 6px', textAlign: 'left',   width: 14 }}></th>
                    <th style={{ padding: '4px 6px', textAlign: 'left'   }}>BOT</th>
                    <th style={{ padding: '4px 6px', textAlign: 'left'   }}>CHAIN</th>
                    <th style={{ padding: '4px 6px', textAlign: 'right'  }}>RPC LAT.</th>
                    <th style={{ padding: '4px 6px', textAlign: 'center' }}>CONN</th>
                    <th style={{ padding: '4px 6px', textAlign: 'right'  }}>TICK INT.</th>
                    <th style={{ padding: '4px 6px', textAlign: 'right'  }}>POS. EST.</th>
                    <th style={{ padding: '4px 6px', textAlign: 'right'  }}>vs ASHBURN</th>
                    <th style={{ padding: '4px 6px', textAlign: 'right'  }}>vs AWS</th>
                  </tr>
                </thead>
                <tbody>
                  {bots.map((b, i) => {
                    const lc = latColor(b.rpc_latency);
                    return (
                      <tr key={b.id} style={{ background: i % 2 ? 'rgba(255,255,255,.02)' : 'transparent', borderBottom: '1px solid #111' }}>
                        <td style={{ padding: '5px 6px' }}>
                          <span style={{ width: 7, height: 7, borderRadius: '50%', background: lc, display: 'inline-block' }} />
                        </td>
                        <td style={{ padding: '5px 6px', fontWeight: 600, color: '#ccc' }}>{b.name}</td>
                        <td style={{ padding: '5px 6px', color: '#777' }}>{b.chain}</td>
                        <td style={{ padding: '5px 6px', textAlign: 'right', fontFamily: 'monospace', fontWeight: 700, color: lc }}>
                          {b.rpc_latency != null ? `${b.rpc_latency}ms` : '—'}
                        </td>
                        <td style={{ padding: '5px 6px', textAlign: 'center' }}>
                          <span className={`badge ${b.connection === 'websocket' ? 'bb' : ''}`} style={{ fontSize: 8 }}>
                            {b.connection === 'websocket' ? 'WS' : 'HTTP'}
                          </span>
                        </td>
                        <td style={{ padding: '5px 6px', textAlign: 'right', fontFamily: 'monospace', color: '#777' }}>
                          {b.tick_interval != null ? `${(b.tick_interval / 1000).toFixed(1)}s` : '—'}
                        </td>
                        <td style={{ padding: '5px 6px', textAlign: 'right', fontFamily: 'monospace', color: '#777' }}>
                          {b.position_pct != null ? `${b.position_pct}%` : '—'}
                        </td>
                        <td style={{ padding: '5px 6px', textAlign: 'right', fontFamily: 'monospace', color: deltaColor(b.vs_ashburn) }}>
                          {fmtDelta(b.vs_ashburn)}
                        </td>
                        <td style={{ padding: '5px 6px', textAlign: 'right', fontFamily: 'monospace', color: deltaColor(b.vs_aws) }}>
                          {fmtDelta(b.vs_aws)}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            {data?.timestamp && (
              <div style={{ fontSize: 9, color: '#444', marginTop: 8, textAlign: 'right' }}>
                medido {data.timestamp.slice(0, 19).replace('T', ' ')} UTC
                {data.bots?.[0]?.rpc_host && ` · ${data.bots[0].rpc_host}`}
              </div>
            )}
          </>
        )}
      </div>

      <div className="panel" style={{ fontSize: 10, color: '#555', lineHeight: 1.7 }}>
        <div className="panel-head">METODOLOGIA</div>
        <div style={{ paddingTop: 6 }}>
          <strong style={{ color: '#777' }}>RPC Lat.:</strong> mediana de 2× eth_blockNumber ao RPC primário da chain (timeout 4s).&nbsp;
          <strong style={{ color: '#777' }}>Tick Int.:</strong> mediana dos intervalos entre ticks consecutivos nos logs systemd (últimos 15min).&nbsp;
          <strong style={{ color: '#777' }}>Pos. Est.:</strong> RPC Lat. / block_time × 100% — % do bloco já decorrido quando recebemos a notificação.&nbsp;
          <strong style={{ color: '#777' }}>vs Ashburn / vs AWS:</strong> delta em relação a latências típicas de us-east-1 para cada chain (estimativas estáticas baseadas em localização de infraestrutura conhecida; negativo = somos mais rápidos).
        </div>
      </div>
    </div>
  );
}

// ─── SYSTEM ───────────────────────────────────────────────────────────────────
function SystemTab({ data, botHealth, onAudit, auditLoading, auditData }) {
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

  const PROTOCOL_COLORS = {
    'Aave V3':     '#B6509E',
    'Compound V3': '#00D395',
    'Morpho Blue': '#2470FF',
    'Moonwell':    '#FF8C00',
    'Ionic':       '#00BCD4',
    'Venus':       '#F0B90B',
  };

  const bots = botHealth?.bots ?? [];
  const liveCount = bots.filter(b => !b.dry_run).length;
  const dryCount  = bots.filter(b => b.dry_run).length;

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

      {bots.length > 0 && (
        <div className="panel mb">
          <div className="panel-head" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <span>LIQUIDATION BOTS ({bots.length})</span>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <span style={{ fontSize: 10, fontWeight: 400 }}>
                <span style={{ color: 'var(--green)' }}>{liveCount} LIVE</span>
                {' · '}
                <span style={{ color: 'var(--yellow)' }}>{dryCount} DRY-RUN</span>
              </span>
              <button
                className={`btn${auditLoading ? ' spin' : ''}`}
                onClick={onAudit}
                disabled={auditLoading}
                style={{ padding: '2px 7px', fontSize: 10 }}
              >
                <RefreshCw size={9} />
                {auditLoading ? ' A AUDITAR...' : ' AUDITAR SISTEMA'}
              </button>
            </div>
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))', gap: 8, marginTop: 10 }}>
            {bots.map(b => {
              const isLive = !b.dry_run;
              const color  = PROTOCOL_COLORS[b.protocol] ?? '#888';
              return (
                <div key={b.id} style={{
                  background: '#1a1a1a',
                  borderRadius: 4,
                  padding: '8px 10px',
                  borderLeft: `3px solid ${isLive ? color : '#555'}`,
                  display: 'flex',
                  flexDirection: 'column',
                  gap: 3,
                }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <span style={{ fontSize: 11, fontWeight: 700, color: '#ddd' }}>{b.name}</span>
                    <span className={`badge ${isLive ? 'bg' : 'by'}`} style={{ fontSize: 9 }}>
                      {isLive ? 'LIVE' : 'DRY'}
                    </span>
                  </div>
                  <div style={{ fontSize: 9, color: '#666' }}>
                    {b.contract
                      ? `${b.contract.slice(0, 6)}…${b.contract.slice(-4)}`
                      : 'no contract'}
                  </div>
                  {(() => {
                    const ad = auditData?.bots?.find(ab => ab.id === b.id)?.audit;
                    if (!ad) return null;
                    const isActive = ad.systemd_status === 'active';
                    const isWs     = ad.connection_type === 'websocket';
                    const e429     = ad.errors_2h?.http_429    ?? 0;
                    const econn    = ad.errors_2h?.no_connection ?? 0;
                    return (
                      <div style={{ marginTop: 4, display: 'flex', flexDirection: 'column', gap: 3, borderTop: '1px solid #2a2a2a', paddingTop: 4 }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
                          <span style={{ width: 5, height: 5, borderRadius: '50%', flexShrink: 0, display: 'inline-block', background: isActive ? 'var(--green)' : 'var(--red)' }} />
                          <span style={{ fontSize: 9, color: '#888' }}>{ad.systemd_status}</span>
                          <span style={{ marginLeft: 2, fontSize: 8, padding: '1px 3px', borderRadius: 2, background: isWs ? 'rgba(0,170,255,.12)' : 'rgba(255,255,255,.05)', color: isWs ? 'var(--blue)' : '#555', border: isWs ? '1px solid rgba(0,170,255,.25)' : '1px solid #2a2a2a' }}>
                            {isWs ? 'WS' : 'HTTP'}
                          </span>
                        </div>
                        <div style={{ fontSize: 9, color: '#666' }}>
                          tick&nbsp;{ad.last_tick ? ad.last_tick.slice(11) : '—'}
                        </div>
                        <div style={{ fontSize: 9 }}>
                          {e429  > 0 && <span style={{ color: 'var(--yellow)' }}>{e429}× 429&nbsp;</span>}
                          {econn > 0 && <span style={{ color: 'var(--yellow)' }}>{econn}× conn&nbsp;</span>}
                          {e429 === 0 && econn === 0 && <span style={{ color: '#333' }}>no errors</span>}
                        </div>
                        {ad.gas?.balance != null && (
                          <div style={{ fontSize: 9, color: 'var(--green)' }}>
                            {ad.gas.balance}&nbsp;{ad.gas.symbol}
                          </div>
                        )}
                      </div>
                    );
                  })()}
                </div>
              );
            })}
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
  { id: 'system',        label: 'System',        icon: Server  },
  { id: 'speed',         label: 'Speed',         icon: Gauge   },
  { id: 'opportunities', label: 'Oportunidades', icon: Search  },
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
  const [liquidationsOp,      setLiquidationsOp]      = useState(null);
  const [liquidationsScroll,       setLiquidationsScroll]       = useState(null);
  const [liquidationsLinea,        setLiquidationsLinea]        = useState(null);
  const [liquidationsCompoundBase,    setLiquidationsCompoundBase]    = useState(null);
  const [liquidationsMorphoBase,      setLiquidationsMorphoBase]      = useState(null);
  const [liquidationsCompoundPolygon, setLiquidationsCompoundPolygon] = useState(null);
  const [liquidationsCompoundArb,     setLiquidationsCompoundArb]     = useState(null);
  const [liquidationsCompoundOp,      setLiquidationsCompoundOp]      = useState(null);
  const [liquidationsMorphoPolygon,   setLiquidationsMorphoPolygon]   = useState(null);
  const [liquidationsMorphoArb,       setLiquidationsMorphoArb]       = useState(null);
  const [gas,                         setGas]                         = useState(null);
  const [botHealth,                   setBotHealth]                   = useState(null);
  const [auditLoading,                setAuditLoading]                = useState(false);
  const [auditData,                   setAuditData]                   = useState(null);
  const [speedData,                   setSpeedData]                   = useState(null);
  const [speedLoading,                setSpeedLoading]                = useState(false);
  const [oppData,                     setOppData]                     = useState(null);
  const [oppLoading,                  setOppLoading]                  = useState(false);
  const [oppWindow,                   setOppWindow]                   = useState('1d');

  const fetchAll = useCallback(async () => {
    setLoading(true);
    const errs = {};
    const [pnlR, ibkrR, sniperR, gridR, fundingR, systemR, flashArbR, liquidationsR, liquidationsPolygonR, liquidationsAvaxR, liquidationsArbR, liquidationsOpR, liquidationsScrollR, liquidationsLineaR, liquidationsCompoundBaseR, liquidationsMorphoBaseR, liquidationsCompoundPolygonR, liquidationsCompoundArbR, liquidationsCompoundOpR, liquidationsMorphoPolygonR, liquidationsMorphoArbR, gasR, botHealthR] = await Promise.allSettled([
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
      apiFetch('/api/liquidations/op'),
      apiFetch('/api/liquidations/scroll'),
      apiFetch('/api/liquidations/linea'),
      apiFetch('/api/liquidations/compound_base'),
      apiFetch('/api/liquidations/morpho_base'),
      apiFetch('/api/liquidations/compound_polygon'),
      apiFetch('/api/liquidations/compound_arb'),
      apiFetch('/api/liquidations/compound_op'),
      apiFetch('/api/liquidations/morpho_polygon'),
      apiFetch('/api/liquidations/morpho_arb'),
      apiFetch('/api/gas'),
      apiFetch('/api/health'),
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
    if (liquidationsOpR.status      === 'fulfilled') setLiquidationsOp(liquidationsOpR.value);
    if (liquidationsScrollR.status       === 'fulfilled') setLiquidationsScroll(liquidationsScrollR.value);
    if (liquidationsLineaR.status        === 'fulfilled') setLiquidationsLinea(liquidationsLineaR.value);
    if (liquidationsCompoundBaseR.status    === 'fulfilled') setLiquidationsCompoundBase(liquidationsCompoundBaseR.value);
    if (liquidationsMorphoBaseR.status      === 'fulfilled') setLiquidationsMorphoBase(liquidationsMorphoBaseR.value);
    if (liquidationsCompoundPolygonR.status === 'fulfilled') setLiquidationsCompoundPolygon(liquidationsCompoundPolygonR.value);
    if (liquidationsCompoundArbR.status     === 'fulfilled') setLiquidationsCompoundArb(liquidationsCompoundArbR.value);
    if (liquidationsCompoundOpR.status      === 'fulfilled') setLiquidationsCompoundOp(liquidationsCompoundOpR.value);
    if (liquidationsMorphoPolygonR.status   === 'fulfilled') setLiquidationsMorphoPolygon(liquidationsMorphoPolygonR.value);
    if (liquidationsMorphoArbR.status       === 'fulfilled') setLiquidationsMorphoArb(liquidationsMorphoArbR.value);
    if (gasR.status                         === 'fulfilled') setGas(gasR.value);
    if (botHealthR.status                   === 'fulfilled') setBotHealth(botHealthR.value);
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

  const handleAudit = useCallback(async () => {
    setAuditLoading(true);
    try {
      const data = await apiFetch('/api/health?full=true');
      setAuditData(data);
    } catch (e) {
      console.error('Audit error:', e);
    } finally {
      setAuditLoading(false);
    }
  }, []);

  const handleMeasureSpeed = useCallback(async () => {
    setSpeedLoading(true);
    try {
      const data = await apiFetch('/api/speed');
      setSpeedData(data);
    } catch (e) {
      console.error('Speed error:', e);
    } finally {
      setSpeedLoading(false);
    }
  }, []);

  const handleFetchOpportunities = useCallback(async (w) => {
    const win = w || oppWindow;
    setOppLoading(true);
    try {
      const data = await apiFetch(`/api/opportunities?window=${win}`);
      setOppData(data);
    } catch (e) {
      console.error('Opportunities error:', e);
    } finally {
      setOppLoading(false);
    }
  }, [oppWindow]);

  const handleOppWindow = useCallback((w) => {
    setOppWindow(w);
    setOppData(null);
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
        {activeTab === 'overview'  && <OverviewTab pnl={pnl} ibkr={ibkr} sniper={sniper} grid={grid} flashArb={flashArb} system={system} liquidationsAll={{ base: liquidations, polygon: liquidationsPolygon, avax: liquidationsAvax, arb: liquidationsArb, op: liquidationsOp, scroll: liquidationsScroll, linea: liquidationsLinea, compound_base: liquidationsCompoundBase, compound_polygon: liquidationsCompoundPolygon, compound_arb: liquidationsCompoundArb, compound_op: liquidationsCompoundOp, morpho_base: liquidationsMorphoBase, morpho_polygon: liquidationsMorphoPolygon, morpho_arb: liquidationsMorphoArb }} gas={gas} errors={errors} />}
        {activeTab === 'ibkr'     && (errors.ibkr    ? <Err msg={errors.ibkr}    /> : <IBKRTab    data={ibkr}    />)}
        {activeTab === 'sniper'   && (errors.sniper  ? <Err msg={errors.sniper}  /> : <SniperTab  data={sniper}  />)}
        {activeTab === 'grid'     && (errors.grid    ? <Err msg={errors.grid}    /> : <GridTab    data={grid}    />)}
        {activeTab === 'funding'  && (errors.funding ? <Err msg={errors.funding} /> : <FundingTab data={funding} />)}
        {activeTab === 'flash-arb' && (errors.flashArb ? <Err msg={errors.flashArb} /> : <FlashArbTab data={flashArb} />)}
        {activeTab === 'liquidations' && (errors.liquidations ? <Err msg={errors.liquidations} /> : <LiquidationsTab dataBase={liquidations} dataPolygon={liquidationsPolygon} dataAvax={liquidationsAvax} dataArb={liquidationsArb} dataOp={liquidationsOp} dataScroll={liquidationsScroll} dataLinea={liquidationsLinea} dataCompoundBase={liquidationsCompoundBase} dataMorphoBase={liquidationsMorphoBase} dataCompoundPolygon={liquidationsCompoundPolygon} dataCompoundArb={liquidationsCompoundArb} dataCompoundOp={liquidationsCompoundOp} dataMorphoPolygon={liquidationsMorphoPolygon} dataMorphoArb={liquidationsMorphoArb} />)}
        {activeTab === 'logs'      && <LogsTab />}
        {activeTab === 'system'   && (errors.system  ? <Err msg={errors.system}  /> : <SystemTab  data={system} botHealth={botHealth} onAudit={handleAudit} auditLoading={auditLoading} auditData={auditData} />)}
        {activeTab === 'speed'         && <SpeedTab data={speedData} onMeasure={handleMeasureSpeed} loading={speedLoading} />}
        {activeTab === 'opportunities' && <OpportunitiesTab data={oppData} onFetch={() => handleFetchOpportunities(oppWindow)} loading={oppLoading} window={oppWindow} onWindow={handleOppWindow} />}
      </main>
    </div>
  );
}
