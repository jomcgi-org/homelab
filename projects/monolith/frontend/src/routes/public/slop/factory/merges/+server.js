import { error, json } from "@sveltejs/kit";

// nosemgrep: sveltekit-server-hardcoded-api-base-fallback
const API_BASE = process.env.API_BASE || "http://localhost:8000";

export async function GET({ fetch, setHeaders }) {
  const res = await fetch(`${API_BASE}/api/agents/public/merges`, {
    signal: AbortSignal.timeout(10_000),
  });
  if (!res.ok) {
    throw error(503, "merge snapshot unavailable");
  }

  const headers = {};
  for (const name of ["cache-control", "etag", "last-modified"]) {
    const value = res.headers?.get?.(name);
    if (value) headers[name] = value;
  }
  setHeaders(headers);

  return json(await res.json());
}
