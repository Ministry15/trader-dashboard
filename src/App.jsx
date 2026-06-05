import React, { useState, useEffect, useCallback, useRef } from 'react'
import {
  AreaChart, Area, BarChart, Bar,
  XAxis, YAxis, Tooltip, ResponsiveContainer,
} from 'recharts'
import {
  Activity, TrendingUp, TrendingDown, Zap, RefreshCw,
  AlertTriangle, CheckCircle, XCircle, Clock, Cpu,
  HardDrive, Server, Database, ArrowUpRight, ArrowDownRight,
  BarChart2, Target, Shield, Grid3x3, Layers,
} from 'lucide-react'

// ─── API ─────────────────────────────────────────────────────────────────────
const API = '/api-proxy'
const HDR = { Authorization: 'Bearer JPxK9m2026TraderB0t!', 'Content-Type': 'application/json' }
const REFRESH = 30_000

async function apiFetch(path) {
  const r = await fetch(API + path, { headers: HDR })
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
  return r.json()
}

// ─── Bot metadata ─────────────────────────────────────────────────────────────
const BOT = {
  grid:          { label: 'WBNB Grid',    pair: 'WBNB/USDT',  chain: 'BSC',    color: '#f59e0b', icon: Grid3x3 },
  pepe_grid:     { label: 'PEPE Grid',    pair: 'PEPE/USDT',  chain: 'BSC',    color: '#22c55e', icon: Grid3x3 },
  solana_grid:   { label: 'SOL Grid',     pair: 'SOL/USDC',   chain: 'Solana', color: '#9945ff', icon: Grid3x3 },
  sniper:        { label: 'BSC Sniper',   pair: 'Any Token',  chain: 'BSC',    color: '#ef4444', icon: Target },
  funding_rate:  { label: 'Funding Rate', pair: 'DOGE/USDT',  chain: 'CEX',    color: '#60a5fa', icon: TrendingUp },
  dca:           { label: 'DCA',          pair: 'WBNB/USDT',  chain: 'BSC',    color: '#e8600a', icon: Layers },
  arbitrage:     { label: 'Arbitrage',    pair: 'Multi-DEX',  chain: 'BSC',    color: '#10b981', icon: Zap },
  cex_grid:      { label: 'CEX Grid',     pair: 'DOGE/USDT',  chain: 'CEX',    color: '#8b5cf6', icon: Grid3x3 },
  solana_sniper: { label: 'SOL Sniper',   pair: 'Any Token',  chain: 'Solana', color: '#ec4899', icon: Target },
}

const PRIMARY = ['grid', 'pepe_grid', 'solana_grid', 'sniper', 'funding_rate']
const TABS = [
  { id: 'overview', label: 'Overview' },
  { id: 'bots',     label: 'Bots' },
  { id: 'trades',   label: 'Trades' },
  { id: 'vps',      label: 'VPS' },
]

// ─── Helpers ──────────────────────────────────────────────────────────────────
const f2   = n => n == null ? '—' : Number(n).toFixed(2)
const fUSD = n => {
  if (n == null) return '—'
  const v = Number(n)
  return (v < 0 ? '-$' : '$') + Math.abs(v).toFixed(2)
}
const fNum = n => n == null ? '—' : Number(n).toLocaleString()
const fPct = n => n == null ? '—' : `${Number(n).toFixed(1)}%`

function clr(n) {
  if (n == null) return 'text-slate-400'
  return Number(n) > 0 ? 'text-profit' : Number(n) < 0 ? 'text-loss' : 'text-slate-400'
}

function ago(ts) {
  if (!ts) return '—'
  const s = Math.floor((Date.now() - new Date(ts + (ts.includes('Z') ? '' : 'Z')).getTime()) / 1000)
  if (s < 0)    return 'just now'
  if (s < 60)   return `${s}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}

function isRecent(ts, minutes = 15) {
  if (!ts) return false
  return (Date.now() - new Date(ts + (ts.includes('Z') ? '' : 'Z')).getTime()) < minutes * 60000
}

// ─── Data hook ───────────────────────────────────────────────────────────────
function useData() {
  const [st, setSt] = useState({ status: null, bots: null, botMap: {}, trades: null, vps: null })
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState(null)
  const [lastRefresh, setLastRefresh] = useState(null)
  const timerRef = useRef(null)

  const refresh = useCallback(async () => {
    try {
      const [status, botsRes, tradesRes, vps] = await Promise.all([
        apiFetch('/api/status'),
        apiFetch('/api/bots'),
        apiFetch('/api/trades?limit=60'),
        apiFetch('/api/vps'),
      ])
      const botMap = {}
      ;(botsRes.bots || []).forEach(b => { botMap[b.bot] = b })
      setSt({ status, bots: botsRes, botMap, trades: tradesRes, vps })
      setLastRefresh(new Date())
      setErr(null)
    } catch (e) {
      setErr(e.message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    refresh()
    timerRef.current = setInterval(refresh, REFRESH)
    return () => clearInterval(timerRef.current)
  }, [refresh])

  return { ...st, loading, err, refresh, lastRefresh }
}

// ─── UI Atoms ────────────────────────────────────────────────────────────────
function Spinner({ size = 16 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" className="animate-spin text-brand">
      <circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" opacity=".25" />
      <path d="M12 2a10 10 0 0 1 10 10" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
    </svg>
  )
}

function StatusDot({ active, pulse = true }) {
  return (
    <span className={`inline-block w-2 h-2 rounded-full shrink-0 ${active ? 'bg-profit' : 'bg-loss'} ${active && pulse ? 'animate-pulse' : ''}`} />
  )
}

function Badge({ children, color = 'slate' }) {
  const map = { slate: 'bg-slate-800 text-slate-300', green: 'bg-profit-muted text-profit', red: 'bg-loss-muted text-loss', amber: 'bg-amber-950 text-warn', blue: 'bg-blue-950 text-info', orange: 'bg-orange-950 text-brand' }
  return <span className={`inline-flex items-center px-1.5 py-0.5 rounded text-xs font-mono font-medium ${map[color] || map.slate}`}>{children}</span>
}

function Card({ children, className = '', onClick }) {
  return (
    <div
      onClick={onClick}
      className={`bg-surface-700 border border-surface-line rounded-lg ${onClick ? 'cursor-pointer hover:border-surface-hover hover:bg-surface-600 transition-colors' : ''} ${className}`}
    >
      {children}
    </div>
  )
}

function SLabel({ children }) {
  return <div className="text-[10px] font-mono text-slate-500 uppercase tracking-widest mb-0.5">{children}</div>
}

function Divider() {
  return <div className="border-t border-surface-line my-3" />
}

function GaugeBar({ pct, color = 'bg-info' }) {
  const capped = Math.min(100, Math.max(0, pct || 0))
  const barColor = capped > 85 ? 'bg-loss' : capped > 65 ? 'bg-warn' : color
  return (
    <div className="h-1.5 w-full bg-surface-500 rounded-full overflow-hidden">
      <div className={`h-full rounded-full transition-all ${barColor}`} style={{ width: `${capped}%` }} />
    </div>
  )
}

// ─── Summary card ─────────────────────────────────────────────────────────────
function KpiCard({ label, value, sub, icon: Icon, color = '', trend }) {
  return (
    <Card className="p-4">
      <div className="flex items-start justify-between mb-2">
        <SLabel>{label}</SLabel>
        {Icon && <Icon size={14} className="text-slate-600 shrink-0" />}
      </div>
      <div className={`text-xl font-mono font-bold tabular ${color || 'text-white'}`}>{value}</div>
      {sub && <div className="text-xs text-slate-500 font-mono mt-1 tabular">{sub}</div>}
      {trend != null && (
        <div className={`flex items-center gap-0.5 mt-1.5 text-xs font-mono ${trend >= 0 ? 'text-profit' : 'text-loss'}`}>
          {trend >= 0 ? <ArrowUpRight size={12} /> : <ArrowDownRight size={12} />}
          {fUSD(Math.abs(trend))} 24h
        </div>
      )}
    </Card>
  )
}

// ─── Bot card ─────────────────────────────────────────────────────────────────
function BotCard({ botId, data }) {
  const meta = BOT[botId] || { label: botId, pair: '?', chain: '?', color: '#94a3b8', icon: Activity }
  const Icon = meta.icon
  const active = isRecent(data?.last_trade, 15)

  return (
    <Card className="p-4 flex flex-col gap-3">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <div className="w-7 h-7 rounded flex items-center justify-center shrink-0" style={{ background: meta.color + '22' }}>
            <Icon size={14} style={{ color: meta.color }} />
          </div>
          <div>
            <div className="text-sm font-semibold text-white leading-tight">{meta.label}</div>
            <div className="text-[10px] font-mono text-slate-500">{meta.pair}</div>
          </div>
        </div>
        <div className="flex items-center gap-1.5 text-right">
          <StatusDot active={active} />
          <Badge color={meta.chain === 'Solana' ? 'blue' : meta.chain === 'CEX' ? 'amber' : 'slate'}>
            {meta.chain}
          </Badge>
        </div>
      </div>

      {/* Metrics */}
      <div className="grid grid-cols-3 gap-x-3 gap-y-2">
        <div>
          <SLabel>Trades</SLabel>
          <div className="text-sm font-mono font-semibold text-white tabular">{fNum(data?.trades_total)}</div>
        </div>
        <div>
          <SLabel>Volume</SLabel>
          <div className="text-sm font-mono text-slate-300 tabular">{fUSD(data?.volume_total)}</div>
        </div>
        <div>
          <SLabel>P&L (sim.)</SLabel>
          <div className={`text-sm font-mono font-semibold tabular ${clr(data?.pnl_total)}`}>
            {fUSD(data?.pnl_total)}
          </div>
        </div>
        <div>
          <SLabel>24h Trades</SLabel>
          <div className="text-sm font-mono text-slate-300 tabular">{data?.trades_24h ?? '—'}</div>
        </div>
        <div>
          <SLabel>24h P&L</SLabel>
          <div className={`text-sm font-mono tabular ${clr(data?.pnl_24h)}`}>{fUSD(data?.pnl_24h)}</div>
        </div>
        <div>
          <SLabel>Rate/h</SLabel>
          <div className="text-sm font-mono text-slate-300 tabular">{data?.trades_1h ?? '—'}/h</div>
        </div>
      </div>

      {/* Footer */}
      <div className="flex items-center justify-between pt-2 border-t border-surface-line">
        <span className="text-[10px] text-slate-600 font-mono">Last: {ago(data?.last_trade)}</span>
        <span className={`text-[10px] font-mono font-medium ${active ? 'text-profit' : 'text-slate-600'}`}>
          {active ? '● LIVE' : '○ IDLE'}
        </span>
      </div>
    </Card>
  )
}

// ─── Trades table ─────────────────────────────────────────────────────────────
function TradeRow({ t }) {
  const meta = BOT[t.bot] || { label: t.bot, color: '#94a3b8' }
  const hasDexBuy  = t.dex_buy  && t.dex_buy  !== 'null'
  const hasDexSell = t.dex_sell && t.dex_sell !== 'null'
  const side = hasDexBuy && !hasDexSell ? 'BUY' : hasDexSell && !hasDexBuy ? 'SELL' : 'BOTH'

  return (
    <tr className="border-b border-surface-line hover:bg-surface-hover transition-colors text-xs font-mono">
      <td className="py-2 px-3 text-slate-500 tabular">{t.id}</td>
      <td className="py-2 px-3 text-slate-400 tabular whitespace-nowrap">{t.ts?.slice(0, 16).replace('T', ' ')}</td>
      <td className="py-2 px-3">
        <span className="font-medium" style={{ color: meta.color }}>{meta.label}</span>
      </td>
      <td className="py-2 px-3 text-slate-300">{t.base}/{t.quote}</td>
      <td className="py-2 px-3">
        <span className={`font-medium ${side === 'BUY' ? 'text-profit' : side === 'SELL' ? 'text-loss' : 'text-info'}`}>
          {side}
        </span>
      </td>
      <td className="py-2 px-3 text-slate-300 tabular text-right">{fUSD(t.size_usd)}</td>
      <td className={`py-2 px-3 tabular text-right font-medium ${clr(t.profit_usd)}`}>{fUSD(t.profit_usd)}</td>
      <td className="py-2 px-3">
        <Badge color={t.status === 'dry_run' ? 'amber' : 'green'}>{t.status}</Badge>
      </td>
    </tr>
  )
}

// ─── VPS gauge ────────────────────────────────────────────────────────────────
function VpsGauge({ label, pct, used, total, unit = '' }) {
  const capped = Math.min(100, Math.max(0, pct || 0))
  const barColor = capped > 85 ? 'text-loss' : capped > 65 ? 'text-warn' : 'text-info'
  return (
    <Card className="p-4">
      <SLabel>{label}</SLabel>
      <div className={`text-2xl font-mono font-bold tabular mb-1 ${barColor}`}>{fPct(pct)}</div>
      <GaugeBar pct={pct} />
      <div className="text-[10px] font-mono text-slate-500 mt-1.5 tabular">
        {used}{unit} / {total}{unit}
      </div>
    </Card>
  )
}

function SvcRow({ name, status }) {
  const active = status === 'active'
  return (
    <div className="flex items-center justify-between py-2 border-b border-surface-line last:border-0">
      <span className="text-sm font-mono text-slate-300">{name}</span>
      <div className="flex items-center gap-1.5">
        <StatusDot active={active} pulse={false} />
        <span className={`text-xs font-mono ${active ? 'text-profit' : 'text-loss'}`}>{status}</span>
      </div>
    </div>
  )
}

// ─── Overview tab ─────────────────────────────────────────────────────────────
function OverviewTab({ bots, botMap, status, vps }) {
  const summary = bots?.summary || {}
  const dryRun  = status?.dry_run ?? true
  const cpuPct  = vps?.cpu_pct ?? 0
  const memPct  = vps?.mem_pct ?? 0

  // mini bar data — trades per bot
  const barData = (bots?.bots || [])
    .filter(b => b.trades_total > 0)
    .sort((a, b) => b.trades_24h - a.trades_24h)
    .slice(0, 8)
    .map(b => ({ name: (BOT[b.bot]?.label || b.bot).split(' ')[0], v: b.trades_24h }))

  return (
    <div className="space-y-6">
      {/* DRY_RUN banner */}
      {dryRun && (
        <div className="flex items-center gap-2 px-4 py-2.5 bg-amber-950/60 border border-warn/30 rounded-lg text-warn text-sm">
          <AlertTriangle size={14} className="shrink-0" />
          <span className="font-medium">DRY RUN MODE</span>
          <span className="text-slate-400 text-xs ml-1">— transacções construídas mas NÃO enviadas. P&L é simulado.</span>
        </div>
      )}

      {/* KPI row */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <KpiCard label="Sim. P&L (Total)" value={fUSD(summary.total_pnl)} color={clr(summary.total_pnl)} trend={null} icon={TrendingUp} />
        <KpiCard label="Total Trades" value={fNum(summary.total_trades)} sub={`${summary.trades_24h ?? '—'} nas últimas 24h`} icon={BarChart2} />
        <KpiCard label="Volume (Total)" value={fUSD(summary.total_volume)} icon={Database} />
        <KpiCard label="CPU / RAM" value={`${fPct(cpuPct)} / ${fPct(memPct)}`} sub={vps ? `Uptime: ${vps.uptime_human}` : '—'} icon={Cpu} />
      </div>

      {/* Primary bots grid */}
      <div>
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-sm font-semibold text-slate-300 uppercase tracking-wider">Primary Bots</h2>
          <span className="text-xs text-slate-500 font-mono">{summary.active_bots ?? '—'} active</span>
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-5 gap-3">
          {PRIMARY.map(id => <BotCard key={id} botId={id} data={botMap[id]} />)}
        </div>
      </div>

      {/* Activity chart */}
      {barData.length > 0 && (
        <div>
          <h2 className="text-sm font-semibold text-slate-300 uppercase tracking-wider mb-3">Trades 24h por Bot</h2>
          <Card className="p-4">
            <ResponsiveContainer width="100%" height={160}>
              <BarChart data={barData} margin={{ top: 4, right: 4, left: -20, bottom: 0 }}>
                <XAxis dataKey="name" tick={{ fill: '#64748b', fontSize: 11, fontFamily: 'JetBrains Mono' }} axisLine={false} tickLine={false} />
                <YAxis tick={{ fill: '#64748b', fontSize: 10, fontFamily: 'JetBrains Mono' }} axisLine={false} tickLine={false} />
                <Tooltip
                  contentStyle={{ background: '#0c0c14', border: '1px solid #1e1e2d', borderRadius: 6, fontSize: 12, fontFamily: 'JetBrains Mono' }}
                  labelStyle={{ color: '#94a3b8' }}
                  cursor={{ fill: '#1e1e2d' }}
                />
                <Bar dataKey="v" fill="#e8600a" radius={[3, 3, 0, 0]} maxBarSize={40} />
              </BarChart>
            </ResponsiveContainer>
          </Card>
        </div>
      )}
    </div>
  )
}

// ─── Bots tab ─────────────────────────────────────────────────────────────────
function BotsTab({ bots, botMap, status }) {
  const allBots = bots?.bots || []
  const primary = PRIMARY.map(id => ({ id, data: botMap[id] }))
  const others  = allBots.filter(b => !PRIMARY.includes(b.bot)).map(b => ({ id: b.bot, data: b }))

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-sm font-semibold text-slate-300 uppercase tracking-wider mb-3">Grid &amp; Strategy Bots</h2>
        <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-3">
          {primary.map(({ id, data }) => <BotCard key={id} botId={id} data={data} />)}
        </div>
      </div>

      {others.length > 0 && (
        <div>
          <h2 className="text-sm font-semibold text-slate-300 uppercase tracking-wider mb-3">Other Bots</h2>
          <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-3">
            {others.map(({ id, data }) => <BotCard key={id} botId={id} data={data} />)}
          </div>
        </div>
      )}

      {/* Enabled bots from config */}
      {status?.enabled_bots?.length > 0 && (
        <Card className="p-4">
          <SLabel>Enabled in orchestrator (settings.yaml)</SLabel>
          <div className="flex flex-wrap gap-1.5 mt-2">
            {status.enabled_bots.map(b => (
              <Badge key={b} color={PRIMARY.includes(b) ? 'orange' : 'slate'}>
                {BOT[b]?.label || b}
              </Badge>
            ))}
          </div>
        </Card>
      )}
    </div>
  )
}

// ─── Trades tab ───────────────────────────────────────────────────────────────
function TradesTab({ trades }) {
  const [filter, setFilter] = useState('all')
  const all = trades?.trades || []
  const bots = [...new Set(all.map(t => t.bot))].sort()
  const shown = filter === 'all' ? all : all.filter(t => t.bot === filter)

  return (
    <div className="space-y-4">
      {/* Filter */}
      <div className="flex gap-2 flex-wrap">
        <button
          onClick={() => setFilter('all')}
          className={`px-3 py-1 rounded text-xs font-mono border transition-colors ${filter === 'all' ? 'bg-brand border-brand text-white' : 'border-surface-line text-slate-400 hover:border-slate-600'}`}
        >all</button>
        {bots.map(b => (
          <button
            key={b}
            onClick={() => setFilter(b)}
            className={`px-3 py-1 rounded text-xs font-mono border transition-colors ${filter === b ? 'border-brand text-brand bg-orange-950/30' : 'border-surface-line text-slate-400 hover:border-slate-600'}`}
          >
            {BOT[b]?.label || b}
          </button>
        ))}
      </div>

      {/* Table */}
      <Card className="overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[700px]">
            <thead>
              <tr className="border-b border-surface-line text-[10px] font-mono text-slate-500 uppercase tracking-widest">
                <th className="py-2 px-3 text-left">ID</th>
                <th className="py-2 px-3 text-left">Timestamp</th>
                <th className="py-2 px-3 text-left">Bot</th>
                <th className="py-2 px-3 text-left">Pair</th>
                <th className="py-2 px-3 text-left">Side</th>
                <th className="py-2 px-3 text-right">Size</th>
                <th className="py-2 px-3 text-right">P&L</th>
                <th className="py-2 px-3 text-left">Status</th>
              </tr>
            </thead>
            <tbody>
              {shown.length === 0 ? (
                <tr><td colSpan={8} className="py-12 text-center text-slate-600 font-mono text-sm">No trades</td></tr>
              ) : (
                shown.map(t => <TradeRow key={t.id} t={t} />)
              )}
            </tbody>
          </table>
        </div>
        <div className="px-3 py-2 border-t border-surface-line text-[10px] font-mono text-slate-600">
          {shown.length} trades {filter !== 'all' ? `(${filter})` : '(all bots)'}
        </div>
      </Card>
    </div>
  )
}

// ─── VPS tab ──────────────────────────────────────────────────────────────────
function VPSTab({ vps, status }) {
  if (!vps) return <div className="text-slate-500 font-mono text-sm py-8 text-center">Carregando métricas...</div>

  const services = vps.services || {}
  const load = vps.load_avg || ['—', '—', '—']

  return (
    <div className="space-y-6">
      {/* Gauges */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <VpsGauge label="CPU" pct={vps.cpu_pct} used={vps.cpu_pct?.toFixed(1)} total={100} unit="%" />
        <VpsGauge label="RAM" pct={vps.mem_pct} used={vps.mem_used_mb} total={vps.mem_total_mb} unit=" MB" />
        <VpsGauge label="Disk" pct={vps.disk?.pct} used={vps.disk?.used} total={vps.disk?.size} />
        <Card className="p-4">
          <SLabel>Uptime</SLabel>
          <div className="text-2xl font-mono font-bold text-white mb-1">{vps.uptime_human}</div>
          <div className="text-[10px] font-mono text-slate-500">
            Load: {load[0]} {load[1]} {load[2]}
          </div>
        </Card>
      </div>

      {/* Services */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <Card className="p-4">
          <SLabel>Services</SLabel>
          <div className="mt-2">
            {Object.entries(services).map(([name, st]) => (
              <SvcRow key={name} name={name} status={st} />
            ))}
          </div>
        </Card>

        <Card className="p-4">
          <SLabel>System Info</SLabel>
          <div className="mt-2 space-y-2 font-mono text-xs">
            <div className="flex justify-between border-b border-surface-line py-1.5">
              <span className="text-slate-500">RAM Total</span>
              <span className="text-white tabular">{vps.mem_total_mb} MB</span>
            </div>
            <div className="flex justify-between border-b border-surface-line py-1.5">
              <span className="text-slate-500">RAM Used</span>
              <span className="text-white tabular">{vps.mem_used_mb} MB</span>
            </div>
            <div className="flex justify-between border-b border-surface-line py-1.5">
              <span className="text-slate-500">RAM Free</span>
              <span className="text-white tabular">{vps.mem_avail_mb} MB</span>
            </div>
            <div className="flex justify-between border-b border-surface-line py-1.5">
              <span className="text-slate-500">Disk Size</span>
              <span className="text-white tabular">{vps.disk?.size}</span>
            </div>
            <div className="flex justify-between border-b border-surface-line py-1.5">
              <span className="text-slate-500">Disk Used</span>
              <span className="text-white tabular">{vps.disk?.used} ({fPct(vps.disk?.pct)})</span>
            </div>
            <div className="flex justify-between py-1.5">
              <span className="text-slate-500">DRY_RUN</span>
              <Badge color={status?.dry_run ? 'amber' : 'red'}>{String(status?.dry_run ?? true)}</Badge>
            </div>
          </div>
        </Card>
      </div>
    </div>
  )
}

// ─── App ──────────────────────────────────────────────────────────────────────
export default function App() {
  const [tab, setTab] = useState('overview')
  const { status, bots, botMap, trades, vps, loading, err, refresh, lastRefresh } = useData()
  const [spinning, setSpinning] = useState(false)

  async function handleRefresh() {
    setSpinning(true)
    await refresh()
    setTimeout(() => setSpinning(false), 600)
  }

  const dryRun = status?.dry_run ?? true

  return (
    <div className="min-h-screen bg-surface-900 font-sans">
      {/* ── Top bar ── */}
      <header className="sticky top-0 z-50 bg-surface-900/95 backdrop-blur border-b border-surface-line">
        <div className="max-w-screen-2xl mx-auto px-4 h-12 flex items-center justify-between gap-3">
          {/* Brand */}
          <div className="flex items-center gap-2 shrink-0">
            <div className="w-5 h-5 rounded bg-brand flex items-center justify-center">
              <Activity size={11} className="text-white" />
            </div>
            <span className="text-sm font-bold text-white tracking-wide hidden sm:block">TRADER</span>
            <span className="text-sm font-bold text-brand tracking-wide hidden sm:block">DASHBOARD</span>
          </div>

          {/* Tabs — horizontal scroll on mobile */}
          <nav className="flex items-center gap-0.5 overflow-x-auto scrollbar-none">
            {TABS.map(t => (
              <button
                key={t.id}
                onClick={() => setTab(t.id)}
                className={`px-3 py-1.5 text-xs font-semibold rounded whitespace-nowrap transition-colors ${
                  tab === t.id
                    ? 'bg-brand/15 text-brand border border-brand/30'
                    : 'text-slate-400 hover:text-slate-200 hover:bg-surface-600'
                }`}
              >
                {t.label}
              </button>
            ))}
          </nav>

          {/* Right controls */}
          <div className="flex items-center gap-2 shrink-0">
            {dryRun && <Badge color="amber">DRY RUN</Badge>}
            {status?.crypto_bsc && (
              <div className="hidden sm:flex items-center gap-1">
                <StatusDot active={status.crypto_bsc === 'active'} />
                <span className="text-[10px] font-mono text-slate-500">bsc</span>
              </div>
            )}
            <button
              onClick={handleRefresh}
              className="p-1.5 rounded hover:bg-surface-600 transition-colors text-slate-400 hover:text-slate-200"
              title="Refresh"
            >
              <RefreshCw size={14} className={spinning ? 'animate-spin' : ''} />
            </button>
            {lastRefresh && (
              <span className="text-[10px] font-mono text-slate-600 hidden md:block tabular">
                {lastRefresh.toLocaleTimeString('pt-PT')}
              </span>
            )}
          </div>
        </div>
      </header>

      {/* ── Main ── */}
      <main className="max-w-screen-2xl mx-auto px-4 py-5">
        {loading && (
          <div className="flex flex-col items-center justify-center py-24 gap-3">
            <Spinner size={28} />
            <span className="text-slate-500 font-mono text-sm">Connecting to API…</span>
          </div>
        )}

        {!loading && err && (
          <div className="flex items-center gap-3 px-4 py-3 bg-loss-muted border border-loss/30 rounded-lg text-loss text-sm">
            <XCircle size={16} className="shrink-0" />
            <div>
              <span className="font-medium">API Error: </span>{err}
              <button onClick={handleRefresh} className="ml-3 underline text-xs">retry</button>
            </div>
          </div>
        )}

        {!loading && !err && (
          <>
            {tab === 'overview' && <OverviewTab bots={bots} botMap={botMap} status={status} vps={vps} />}
            {tab === 'bots'     && <BotsTab bots={bots} botMap={botMap} status={status} />}
            {tab === 'trades'   && <TradesTab trades={trades} />}
            {tab === 'vps'      && <VPSTab vps={vps} status={status} />}
          </>
        )}
      </main>

      {/* ── Footer ── */}
      <footer className="border-t border-surface-line mt-12 px-4 py-3 max-w-screen-2xl mx-auto flex items-center justify-between text-[10px] font-mono text-slate-700">
        <span>crypto_bsc · BSC Mainnet · chain_id 56</span>
        <span className="tabular">{new Date().toISOString().slice(0, 10)}</span>
      </footer>
    </div>
  )
}
