/**
 * api/proxy.js — Vercel Serverless Proxy
 *
 * Recebe GET /api/proxy?path=<encoded-vps-path> do frontend React,
 * adiciona Authorization: Bearer server-side e faz forward para o VPS.
 * O VPS nunca precisa de ter a porta pública — só os servidores Vercel a atingem.
 *
 * Uso no frontend:
 *   fetch('/api/proxy?path=' + encodeURIComponent('/api/bots'))
 */

const VPS    = 'http://178.104.133.71:80'
const TOKEN  = 'Bearer JPxK9m2026TraderB0t!'
const TIMEOUT = 15_000   // ms

/** Paths permitidos — whitelist de segurança (nunca expõe endpoints internos) */
function allowed(path) {
  return path === '/health' || path.startsWith('/api/')
}

export default async function handler(req, res) {
  res.setHeader('Access-Control-Allow-Origin',  '*')
  res.setHeader('Access-Control-Allow-Methods', 'GET, OPTIONS')
  res.setHeader('Cache-Control', 'no-store, max-age=0')

  if (req.method === 'OPTIONS') return res.status(204).end()

  const rawPath = req.query.path
  if (!rawPath) {
    return res.status(400).json({
      error: 'missing_path',
      message: 'query param ?path= is required (e.g. /api/proxy?path=/api/bots)',
    })
  }

  if (!allowed(rawPath)) {
    return res.status(403).json({
      error: 'forbidden_path',
      message: `path "${rawPath}" is not whitelisted`,
    })
  }

  const target = `${VPS}${rawPath}`
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), TIMEOUT)

  try {
    const upstream = await fetch(target, {
      method:  'GET',
      headers: { Authorization: TOKEN },
      signal:  controller.signal,
    })
    clearTimeout(timer)

    const body = await upstream.json()
    return res.status(upstream.status).json(body)

  } catch (err) {
    clearTimeout(timer)
    const isTimeout = err.name === 'AbortError' || err.name === 'TimeoutError'
    return res.status(isTimeout ? 504 : 502).json({
      error:   isTimeout ? 'gateway_timeout' : 'bad_gateway',
      message: isTimeout
        ? `VPS não respondeu em ${TIMEOUT / 1000}s`
        : err.message,
    })
  }
}
