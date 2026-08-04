// No URL fallback: API_BASE is injected via values.yaml in prod; a localhost
// dev run sets it in the environment. A missing var fails loudly instead of
// silently serving from the wrong backend.
const API_BASE = process.env.API_BASE;

export async function load({ fetch }) {
  try {
    const res = await fetch(`${API_BASE}/api/agents/sessions`, {
      signal: AbortSignal.timeout(15000),
    });
    if (!res.ok) return { sessions: [], error: true };
    const body = await res.json();
    return {
      sessions: Array.isArray(body) ? body : (body.sessions ?? []),
      error: false,
    };
  } catch {
    return { sessions: [], error: true };
  }
}
