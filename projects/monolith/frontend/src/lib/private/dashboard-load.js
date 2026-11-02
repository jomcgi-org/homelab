// One read for the private root: the home aggregate plus the factory board,
// fetched together and merged so the page and its refresh endpoint carry the
// same shape. Each source fails soft on its own: a dead factory leaves the
// calendar standing, and a dead home aggregate still reports the factory.
import { summarizeBoard } from "./factory-summary.js";

const HOME_TIMEOUT_MS = 15000;
const FACTORY_TIMEOUT_MS = 15000;

async function readJson(fetch, url, timeout) {
  try {
    const res = await fetch(url, { signal: AbortSignal.timeout(timeout) });
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

/**
 * `{ dashboard }`, where dashboard is the home aggregate with a `factory`
 * summary attached, or null when neither source answered.
 */
export async function loadDashboard(fetch, apiBase) {
  const [home, board] = await Promise.all([
    readJson(fetch, `${apiBase}/api/home/dashboard`, HOME_TIMEOUT_MS),
    readJson(fetch, `${apiBase}/api/agents/factory`, FACTORY_TIMEOUT_MS),
  ]);
  const factory = summarizeBoard(board);
  if (home == null && factory == null) return { dashboard: null };
  if (home != null && typeof home === "object" && home.error) {
    return { dashboard: null };
  }
  return { dashboard: { ...(home ?? {}), factory } };
}
