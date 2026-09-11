// Same contract as ../factory/+page.server.js: API_BASE is injected via
// values.yaml in prod and set in the environment for a local run. The board
// carries the escalation list, so this needs no second endpoint; the page then
// polls /agents/escalations itself.
const API_BASE = process.env.API_BASE;

export async function load({ fetch }) {
  try {
    const response = await fetch(`${API_BASE}/api/agents/factory`, {
      signal: AbortSignal.timeout(15000),
    });
    if (!response.ok) {
      return { escalations: [], error: true };
    }
    const board = await response.json();
    return { escalations: board.escalations ?? [], error: false };
  } catch {
    return { escalations: [], error: true };
  }
}
