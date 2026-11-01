import { loadDashboard } from "$lib/private/dashboard-load.js";

const API_BASE = process.env.API_BASE || "http://localhost:8000";

/**
 * GET proxy for the dashboard read: the backend home aggregate with the
 * factory summary attached, the same shape the page's load returns. The
 * page's 60s client refresh hits this same-origin endpoint instead of the
 * cluster-internal API base.
 */
export async function GET({ fetch }) {
  const { dashboard } = await loadDashboard(fetch, API_BASE);
  if (dashboard == null) {
    return new Response(JSON.stringify({ error: "dashboard unavailable" }), {
      status: 502,
      headers: { "Content-Type": "application/json" },
    });
  }
  return new Response(JSON.stringify(dashboard), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}
