import { notConfigured, qwenApiBase } from "../upstream.js";

async function getJson(url) {
  const res = await fetch(url, { signal: AbortSignal.timeout(5000) });
  if (!res.ok) throw new Error(`${url} answered ${res.status}`);
  return res.json();
}

// Live server state plus the most recent requests (server-side duration and
// TTFT for each turn, without the browser's network hop).
export async function GET() {
  const base = qwenApiBase();
  if (!base) return notConfigured();
  try {
    const [stats, requests] = await Promise.all([
      getJson(`${base}/v1/stats`),
      getJson(`${base}/v1/requests?limit=20`),
    ]);
    return new Response(
      JSON.stringify({ stats, recent: requests.entries ?? [] }),
      { headers: { "Content-Type": "application/json" } },
    );
  } catch (err) {
    return new Response(JSON.stringify({ error: err.message }), {
      status: 502,
      headers: { "Content-Type": "application/json" },
    });
  }
}
