// One read for the private root: the home aggregate plus a summary of the
// factory board, merged so the page and its refresh endpoint carry the same
// shape. Both come from the process-wide backend cache (backend-cache.js), so
// the page answers from memory and never waits on a slow or stalled backend.
// Each source fails soft on its own: a dead factory leaves the calendar
// standing, and a dead home aggregate still reports the factory.
import {
  backendCache,
  boardStatus,
  readBoard,
  readHome,
} from "./backend-cache.js";
import { summarizeBoard } from "./factory-summary.js";

/**
 * `{ dashboard }`, where dashboard is the home aggregate with a `factory`
 * summary attached, or null when neither source has a reading.
 */
export async function loadDashboard(apiBase, { cache } = {}) {
  const options = cache ? { cache } : {};
  const [home, board] = await Promise.all([
    readHome(apiBase, options),
    readBoard(apiBase, options),
  ]);
  const factory = summarize(board);
  const status = boardStatus(cache ?? backendCache);
  // No board yet but a first read on the way: the page says "reading", not
  // "unavailable", and asks again shortly. Once a read has failed, a board
  // still missing is unavailable, however many retries are in flight.
  const factory_pending = factory == null && status.pending && !status.failed;
  if (home == null && factory == null && !factory_pending)
    return { dashboard: null };
  return {
    dashboard: {
      ...(home ?? {}),
      factory:
        factory && status.ageMs != null
          ? { ...factory, readAgoMs: status.ageMs }
          : factory,
      factory_pending,
    },
  };
}

// A malformed board reads as no factory reading, never as a broken page.
function summarize(board) {
  try {
    return summarizeBoard(board);
  } catch {
    return null;
  }
}
