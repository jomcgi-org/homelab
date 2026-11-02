// One read for the private root: the home aggregate plus a summary of the
// factory board, merged so the page and its refresh endpoint carry the same
// shape. Each source fails soft on its own: a dead factory leaves the
// calendar standing, and a dead home aggregate still reports the factory.
//
// The board read is slow (25-35s on the hub: it enriches every in-flight
// task), longer than the gateway's 15s request timeout. The page never waits
// for it. This server process keeps the last summary and refreshes it in the
// background, one read at a time and at most once a minute, so every
// dashboard request answers from memory and the backend does one board read
// a minute however many tabs are open.
import { summarizeBoard } from "./factory-summary.js";

const HOME_TIMEOUT_MS = 10000;
const BOARD_TIMEOUT_MS = 90000;
/** A summary younger than this is served without starting a refresh. */
export const BOARD_FRESH_MS = 60000;
/** A summary older than this is no longer a reading: the factory reads unavailable. */
export const BOARD_STALE_MS = 300000;

async function readJson(fetch, url, timeout) {
  try {
    const res = await fetch(url, { signal: AbortSignal.timeout(timeout) });
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

let cached = null; // { factory, at }
let pending = null;
let attemptedAt = -Infinity;

/** Test hook: forget the cached summary and any read in flight. */
export function resetBoardCache() {
  cached = null;
  pending = null;
  attemptedAt = -Infinity;
}

function refreshBoard(fetch, apiBase, now) {
  if (pending) return pending;
  attemptedAt = now();
  pending = readJson(fetch, `${apiBase}/api/agents/factory`, BOARD_TIMEOUT_MS)
    .then((board) => {
      const factory = summarizeBoard(board);
      if (factory) cached = { factory, at: now() };
    })
    // Nobody awaits this read: a malformed board must not become an
    // unhandled rejection, which would end the whole server process.
    .catch(() => {})
    .finally(() => {
      pending = null;
    });
  return pending;
}

/**
 * The factory summary this process holds, or null when it has none younger
 * than BOARD_STALE_MS. Starts a background refresh when the summary is older
 * than BOARD_FRESH_MS; never waits for it.
 */
export function factorySummary(fetch, apiBase, now = Date.now) {
  const age = cached ? now() - cached.at : Infinity;
  // Attempts are spaced too, so a board that fails fast is still read at
  // most once a minute.
  if (age >= BOARD_FRESH_MS && now() - attemptedAt >= BOARD_FRESH_MS)
    refreshBoard(fetch, apiBase, now);
  return age < BOARD_STALE_MS ? cached.factory : null;
}

/**
 * `{ dashboard }`, where dashboard is the home aggregate with a `factory`
 * summary attached, or null when neither source answered. `boardFetch` makes
 * the background board read; it outlives the request, so it is the global
 * fetch rather than the request's own.
 */
export async function loadDashboard(
  fetch,
  apiBase,
  { boardFetch = globalThis.fetch, now = Date.now } = {},
) {
  const factory = factorySummary(boardFetch, apiBase, now);
  const aggregate = await readJson(
    fetch,
    `${apiBase}/api/home/dashboard`,
    HOME_TIMEOUT_MS,
  );
  // A 200 that carries {error} is the aggregate saying it has nothing, the
  // same as a 503 from it: that source is down, the other still counts.
  const home =
    aggregate && typeof aggregate === "object" && !aggregate.error
      ? aggregate
      : null;
  if (home == null && factory == null) return { dashboard: null };
  return { dashboard: { ...(home ?? {}), factory } };
}
