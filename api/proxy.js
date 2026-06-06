const API_KEY = process.env.TRADER_API_KEY || 'JPxK9m2026TraderB0t!';
const API_BASE = 'http://178.104.133.71:5000';

export default async function handler(req, res) {
  const path = req.query.path;
  if (!path) return res.status(400).json({ error: 'missing ?path=' });

  try {
    const upstream = await fetch(`${API_BASE}${path}`, {
      headers: { 'Authorization': `Bearer ${API_KEY}` },
    });
    const data = await upstream.json();
    res.status(upstream.status).json(data);
  } catch (err) {
    res.status(502).json({ error: 'upstream error', detail: err.message });
  }
}
