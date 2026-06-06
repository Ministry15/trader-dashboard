const API_KEY  = process.env.TRADER_API_KEY || 'JPxK9m2026TraderB0t!';
const API_BASE = 'http://178.104.133.71:8000';
const ANT_KEY  = process.env.VITE_ANTHROPIC_KEY;
const ANT_URL  = 'https://api.anthropic.com/v1/messages';

async function get(path) {
  try {
    const r = await fetch(`${API_BASE}${path}`, {
      headers: { 'x-api-key': API_KEY },
      signal: AbortSignal.timeout(8000),
    });
    return r.ok ? r.json() : null;
  } catch { return null; }
}

function stamp() {
  return new Date().toISOString().replace('T', ' ').slice(0, 19) + ' UTC';
}

function pct(a, b) {
  if (!b) return '—';
  return ((a / b) * 100).toFixed(1) + '%';
}

function fmtUSD(n) {
  if (n == null) return '—';
  const v = parseFloat(n);
  return (v < 0 ? '-$' : '$') + Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

// ── CSS shared between both modes ─────────────────────────────────────────────
const SHARED_CSS = `
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:#0a0a0a;color:#ccffe6;font-family:'JetBrains Mono',monospace;font-size:14px;line-height:1.65;padding:20px 16px;max-width:960px;margin:0 auto}
  h1{color:#00ff88;font-size:18px;letter-spacing:.1em;margin-bottom:4px}
  .ts{color:#555;font-size:11px;margin-bottom:24px}
  .badge{display:inline-block;padding:2px 10px;border-radius:2px;font-size:10px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;margin-left:12px;vertical-align:middle}
  .badge-ai{background:rgba(0,255,136,.15);color:#00ff88;border:1px solid rgba(0,255,136,.4)}
  .badge-auto{background:rgba(0,170,255,.15);color:#00aaff;border:1px solid rgba(0,170,255,.4)}
  details{background:#111;border:1px solid #1a1a1a;border-radius:4px;margin-bottom:12px;overflow:hidden}
  summary{padding:12px 16px;cursor:pointer;font-size:11px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#00ff88;display:flex;align-items:center;gap:8px;list-style:none;user-select:none}
  summary::-webkit-details-marker{display:none}
  summary::before{content:'▶';font-size:9px;transition:transform .15s;flex-shrink:0}
  details[open] summary::before{transform:rotate(90deg)}
  details[open] summary{border-bottom:1px solid #1a1a1a}
  .sec-body{padding:16px}
  .row{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;padding:8px 0;border-bottom:1px solid #1a1a1a;font-size:12px}
  .row:last-child{border-bottom:none}
  .row-name{color:#00ff88;font-weight:700;min-width:130px;flex-shrink:0}
  .row-body{flex:1;color:#aaa}
  .tag{display:inline-block;padding:1px 7px;border-radius:2px;font-size:10px;font-weight:700;letter-spacing:.06em;margin:0 3px}
  .ok{background:rgba(0,255,136,.1);color:#00ff88;border:1px solid rgba(0,255,136,.25)}
  .warn{background:rgba(255,204,0,.1);color:#fc0;border:1px solid rgba(255,204,0,.25)}
  .alrt{background:rgba(255,120,0,.12);color:#ff7800;border:1px solid rgba(255,120,0,.3)}
  .err{background:rgba(255,51,85,.1);color:#ff3355;border:1px solid rgba(255,51,85,.25)}
  .cards{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px}
  .card{background:#161616;border:1px solid #1a1a1a;border-radius:3px;padding:10px 16px;min-width:140px}
  .card-lbl{font-size:10px;color:#555;letter-spacing:.1em;text-transform:uppercase;margin-bottom:4px}
  .card-val{font-size:20px;font-weight:700;color:#00ff88}
  .card-val.warn{color:#fc0}
  .card-val.alrt{color:#ff7800}
  .card-val.err{color:#ff3355}
  .rec-list{list-style:none;counter-reset:rec;padding:0}
  .rec-list li{counter-increment:rec;padding:10px 0;border-bottom:1px solid #1a1a1a;font-size:12px;color:#ccffe6}
  .rec-list li:last-child{border-bottom:none}
  .rec-list li::before{content:counter(rec)". ";color:#00ff88;font-weight:700}
  .none{color:#555;font-size:12px;padding:4px 0}
</style>`;

// ── Rule-based report ─────────────────────────────────────────────────────────
function buildRulesHtml(data) {
  const { bots, trades, liquidations, vps, sniper, grid } = data;
  const now = stamp();
  const recs = [];

  // ── Grid diagnosis ──────────────────────────────────────────────────────────
  const activeGrids = grid?.active_grids ?? bots?.active_grids ?? [];
  const recentTrades = trades?.trades ?? [];
  let gridRows = '';
  for (const g of activeGrids) {
    const name  = g.bot ?? '?';
    const price = parseFloat(g.price ?? 0);
    const lower = parseFloat(g.lower ?? 0);
    const upper = parseFloat(g.upper ?? 0);
    const inRange = lower > 0 && price >= lower && price <= upper;
    const statusTag = inRange
      ? `<span class="tag ok">IN RANGE</span>`
      : `<span class="tag err">OUT OF RANGE</span>`;
    const tc = recentTrades.filter(t => (t.bot ?? '').toLowerCase() === name.toLowerCase()).length;
    gridRows += `<div class="row"><div class="row-name">${name}</div><div class="row-body">${statusTag} preço <b>${price.toFixed(4)}</b> | range [${lower.toFixed(4)}..${upper.toFixed(4)}] | ${tc} trades recentes</div></div>`;
    if (!inRange && lower > 0)
      recs.push(`Recentrar grid <b>${name}</b> — preço ${price.toFixed(4)} fora do range [${lower.toFixed(4)}..${upper.toFixed(4)}].`);
  }
  const gridHtml = gridRows || '<div class="none">Sem dados de grid activos.</div>';

  // ── Liquidations ────────────────────────────────────────────────────────────
  const allOpps    = liquidations?.opportunities ?? [];
  // HF > 1.2 → saudável, não mostrar
  const opps    = allOpps.filter(o => parseFloat(o.health_factor) < 1.2);
  const urgent  = opps.filter(o => parseFloat(o.health_factor) < 1.0);
  const alrt    = opps.filter(o => { const hf = parseFloat(o.health_factor); return hf >= 1.0 && hf < 1.05; });
  const monitor = opps.filter(o => { const hf = parseFloat(o.health_factor); return hf >= 1.05 && hf < 1.2; });
  const totalEstProfit = liquidations?.summary?.total_est_profit ?? opps.reduce((s, o) => s + parseFloat(o.estimated_profit ?? 0), 0);

  let liqRows = '';
  for (const o of urgent) {
    liqRows += `<div class="row"><div class="row-name"><span class="tag err">URGENTE</span></div><div class="row-body"><span style="font-size:11px;color:#888">${(o.position_address ?? '').slice(0, 14)}…</span> HF=<b style="color:#ff3355">${parseFloat(o.health_factor).toFixed(4)}</b> dívida=${fmtUSD(o.debt_usd)} lucro≈${fmtUSD(o.estimated_profit)}</div></div>`;
  }
  for (const o of alrt) {
    liqRows += `<div class="row"><div class="row-name"><span class="tag alrt">ALERTA</span></div><div class="row-body"><span style="font-size:11px;color:#888">${(o.position_address ?? '').slice(0, 14)}…</span> HF=<b style="color:#ff7800">${parseFloat(o.health_factor).toFixed(4)}</b> dívida=${fmtUSD(o.debt_usd)} lucro≈${fmtUSD(o.estimated_profit)}</div></div>`;
  }
  for (const o of monitor) {
    liqRows += `<div class="row"><div class="row-name"><span class="tag warn">MONITOR</span></div><div class="row-body"><span style="font-size:11px;color:#888">${(o.position_address ?? '').slice(0, 14)}…</span> HF=<b style="color:#fc0">${parseFloat(o.health_factor).toFixed(4)}</b> dívida=${fmtUSD(o.debt_usd)} lucro≈${fmtUSD(o.estimated_profit)}</div></div>`;
  }

  const liqCards = `
    <div class="cards">
      <div class="card"><div class="card-lbl">HF &lt; 1.0 — Urgente</div><div class="card-val${urgent.length > 0 ? ' err' : ''}">${urgent.length}</div></div>
      <div class="card"><div class="card-lbl">HF 1.0–1.05 — Alerta</div><div class="card-val${alrt.length > 0 ? ' alrt' : ''}">${alrt.length}</div></div>
      <div class="card"><div class="card-lbl">HF 1.05–1.2 — Monitor</div><div class="card-val${monitor.length > 0 ? ' warn' : ''}">${monitor.length}</div></div>
      <div class="card"><div class="card-lbl">Lucro Estimado</div><div class="card-val">${fmtUSD(totalEstProfit)}</div></div>
    </div>${liqRows || '<div class="none">Sem posições em risco (HF &lt; 1.2).</div>'}`;

  if (urgent.length > 0)
    recs.push(`${urgent.length} posição(ões) Aave com HF&lt;1.0 — liquidável AGORA (lucro estimado: ${fmtUSD(urgent.reduce((s,o) => s + parseFloat(o.estimated_profit ?? 0), 0))}).`);
  if (alrt.length > 0)
    recs.push(`${alrt.length} posição(ões) Aave com HF 1.0–1.05 — liquidação iminente, activar bot em modo live.`);
  if (monitor.length > 0)
    recs.push(`${monitor.length} posição(ões) Aave com HF 1.05–1.2 — monitorizar de perto nas próximas horas.`);

  // ── Sniper ──────────────────────────────────────────────────────────────────
  const winRate  = sniper?.win_rate ?? null;
  const wins     = sniper?.wins     ?? 0;
  const losses   = sniper?.losses   ?? 0;
  const totalPnl = sniper?.total_pnl_usdt ?? null;
  const sniperCards = `
    <div class="cards">
      <div class="card"><div class="card-lbl">Win Rate</div><div class="card-val${winRate != null && winRate < 40 ? ' warn' : ''}">${winRate != null ? winRate.toFixed(1) + '%' : '—'}</div></div>
      <div class="card"><div class="card-lbl">W / L</div><div class="card-val">${wins} / ${losses}</div></div>
      <div class="card"><div class="card-lbl">P&L USDT (7d)</div><div class="card-val${totalPnl != null && totalPnl < 0 ? ' err' : ''}">${fmtUSD(totalPnl)}</div></div>
      <div class="card"><div class="card-lbl">Total Trades</div><div class="card-val">${sniper?.total_trades ?? 0}</div></div>
    </div>`;
  if (winRate != null && winRate < 40)
    recs.push(`Win rate do sniper em ${winRate.toFixed(1)}% — rever filtros de entrada ou aumentar take-profit.`);

  // ── Efficiency ──────────────────────────────────────────────────────────────
  const detected = liquidations?.summary?.total ?? opps.length;
  const executed = liquidations?.summary?.executed ?? opps.filter(o => o.executed).length;
  const allBots  = bots?.bots ?? [];
  const dryBots  = allBots.filter(b => b.dry_run === true || b.dry_run === 'true' || b.status === 'dry_run');

  const effCards = `
    <div class="cards">
      <div class="card"><div class="card-lbl">Opps Detectadas</div><div class="card-val">${detected}</div></div>
      <div class="card"><div class="card-lbl">Opps Executadas</div><div class="card-val">${executed}</div></div>
      <div class="card"><div class="card-lbl">Taxa de Execução</div><div class="card-val${detected > 5 && executed === 0 ? ' warn' : ''}">${pct(executed, detected)}</div></div>
      <div class="card"><div class="card-lbl">Bots em DRY_RUN</div><div class="card-val warn">${dryBots.length}</div></div>
    </div>`;
  if (dryBots.length > 0)
    recs.push(`${dryBots.length} bot(s) em DRY_RUN: ${dryBots.map(b => b.name ?? b.bot ?? '?').join(', ')} — activar após validação.`);

  // ── System health ───────────────────────────────────────────────────────────
  const cpu  = vps?.cpu_percent     ?? null;
  const ram  = vps?.memory_used_pct ?? null;
  const svcs = Array.isArray(vps?.services)
    ? vps.services
    : Object.entries(vps?.services ?? {}).map(([k, v]) => ({ service: k, active: v === 'active' || v === true, status: v }));
  const failed = svcs.filter(s => !s.active && (s.status ?? '') !== 'inactive');

  let svcRows = '';
  for (const s of failed) svcRows += `<div class="row"><div class="row-name"><span class="tag err">DOWN</span></div><div class="row-body" style="color:#aaa">${s.service ?? s.name}</div></div>`;
  for (const s of svcs.filter(s => s.active)) svcRows += `<div class="row"><div class="row-name"><span class="tag ok">UP</span></div><div class="row-body" style="color:#555">${s.service ?? s.name}</div></div>`;

  const sysCards = `
    <div class="cards">
      <div class="card"><div class="card-lbl">CPU</div><div class="card-val${cpu >= 90 ? ' err' : cpu >= 70 ? ' warn' : ''}">${cpu != null ? cpu.toFixed(1) + '%' : '—'}</div></div>
      <div class="card"><div class="card-lbl">RAM</div><div class="card-val${ram >= 90 ? ' err' : ram >= 70 ? ' warn' : ''}">${ram != null ? ram.toFixed(1) + '%' : '—'}</div></div>
      <div class="card"><div class="card-lbl">Serviços em Falha</div><div class="card-val${failed.length > 0 ? ' err' : ''}">${failed.length}</div></div>
    </div>${svcRows}`;

  if (failed.length > 0)
    recs.push(`Serviços em falha: ${failed.map(s => s.service ?? s.name).join(', ')} — verificar com <code>systemctl status</code>.`);
  if (cpu != null && cpu >= 85)
    recs.push(`CPU em ${cpu.toFixed(1)}% — incomum para o padrão de carga actual.`);
  if (ram != null && ram >= 85)
    recs.push(`RAM em ${ram.toFixed(1)}% — verificar possíveis memory leaks no orquestrador.`);

  const top5    = recs.slice(0, 5);
  const recHtml = top5.length
    ? `<ul class="rec-list">${top5.map(r => `<li>${r}</li>`).join('')}</ul>`
    : '<div class="none">Sistema a operar normalmente — sem recomendações urgentes.</div>';

  const liqOpen = urgent.length > 0 || alrt.length > 0 || monitor.length > 0;
  const sysOpen = failed.length > 0 || (cpu != null && cpu >= 70) || (ram != null && ram >= 70);

  return `<!DOCTYPE html>
<html lang="pt">
<head><meta charset="UTF-8"><title>Trader Report — ${now}</title>${SHARED_CSS}</head>
<body>
  <h1>TRADER REPORT <span class="badge badge-auto">Auto Report</span></h1>
  <div class="ts">Gerado em ${now} · Análise automática por regras</div>

  <details open>
    <summary>Diagnóstico — Grid Bots</summary>
    <div class="sec-body">${gridHtml}</div>
  </details>

  <details ${liqOpen ? 'open' : ''}>
    <summary>Liquidações Aave V3 ${urgent.length > 0 ? `<span class="tag err">${urgent.length} URGENTE</span>` : alrt.length > 0 ? `<span class="tag alrt">${alrt.length} ALERTA</span>` : ''}</summary>
    <div class="sec-body">${liqCards}</div>
  </details>

  <details>
    <summary>Sniper BSC</summary>
    <div class="sec-body">${sniperCards}</div>
  </details>

  <details>
    <summary>Eficiência do Sistema</summary>
    <div class="sec-body">${effCards}</div>
  </details>

  <details ${sysOpen ? 'open' : ''}>
    <summary>Saúde do Sistema ${failed.length > 0 ? `<span class="tag err">${failed.length} DOWN</span>` : ''}</summary>
    <div class="sec-body">${sysCards}</div>
  </details>

  <details open>
    <summary>Recomendações (${top5.length})</summary>
    <div class="sec-body">${recHtml}</div>
  </details>
</body>
</html>`;
}

// ── Anthropic API call ────────────────────────────────────────────────────────
async function callAnthropic(data) {
  const system = `És um analista de trading algorítmico sénior. Analisa estes dados e gera relatório HTML dark theme verde néon com: diagnóstico por bot, eficiência do sistema, alertas urgentes e máximo 5 recomendações accionáveis. Nunca repetes dados já visíveis no dashboard — só insights e diagnósticos.\n\nO HTML deve incluir:\n- <style> próprio com background #0a0a0a, cor principal #00ff88, font monospace\n- Secções colapsáveis <details>/<summary>\n- Badge "AI Report" com estilo neon verde no título\n- Timestamp no topo\nRetorna APENAS o HTML completo (<!DOCTYPE html>…</html>), sem markdown, sem blocos de código.`;

  const resp = await fetch(ANT_URL, {
    method: 'POST',
    headers: {
      'x-api-key': ANT_KEY,
      'anthropic-version': '2023-06-01',
      'content-type': 'application/json',
    },
    body: JSON.stringify({
      model: 'claude-haiku-4-5-20251001',
      max_tokens: 4096,
      system,
      messages: [{ role: 'user', content: `Dados do sistema:\n${JSON.stringify(data, null, 2)}` }],
    }),
    signal: AbortSignal.timeout(30000),
  });

  if (resp.status === 429 || resp.status === 402) {
    throw new Error(`anthropic_${resp.status}`);
  }
  if (!resp.ok) {
    const e = await resp.json().catch(() => ({}));
    throw new Error(e.error?.message ?? `HTTP ${resp.status}`);
  }

  const result = await resp.json();
  const text = result.content?.[0]?.text ?? '';
  if (!text.includes('<html')) throw new Error('invalid_html');
  return text;
}

// ── Handler ───────────────────────────────────────────────────────────────────
export default async function handler(req, res) {
  res.setHeader('Access-Control-Allow-Origin', '*');

  const [bots, trades, liquidations, vps, sniper, grid] = await Promise.all([
    get('/api/bots'),
    get('/api/trades?limit=100'),
    get('/api/liquidations'),
    get('/api/vps'),
    get('/api/sniper'),
    get('/api/grid'),
  ]);

  const data = { bots, trades, liquidations, vps, sniper, grid };

  if (ANT_KEY) {
    try {
      const html = await callAnthropic(data);
      return res.json({ html, mode: 'ai' });
    } catch (err) {
      console.warn('[report] Anthropic fallback:', err.message);
    }
  }

  return res.json({ html: buildRulesHtml(data), mode: 'rules' });
}
