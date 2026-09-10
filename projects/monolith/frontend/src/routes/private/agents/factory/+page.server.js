// Same contract as ../+page.server.js: API_BASE is injected via values.yaml
// in prod and set in the environment for a local run; a missing var fails
// loudly rather than serving from the wrong backend. The board loads once
// here for a fast first paint; the page then polls /agents/factory itself.
const API_BASE = process.env.API_BASE;

export async function load({ fetch, url, untrack }) {
  // untrack: the page owns ?task= after the first paint (toggle + goto with
  // replaceState), so the load must not depend on it or every toggle would
  // re-run this fetch and discard the result.
  const task = untrack(() => url.searchParams.get("task"));
  const query = task ? `?task=${encodeURIComponent(task)}` : "";
  try {
    const response = await fetch(`${API_BASE}/api/agents/factory${query}`, {
      signal: AbortSignal.timeout(15000),
    });
    if (!response.ok) {
      return { board: null, task, error: true };
    }
    return { board: await response.json(), task, error: false };
  } catch {
    return { board: null, task, error: true };
  }
}
