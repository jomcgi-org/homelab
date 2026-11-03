// Stale-while-revalidate reads of the private backend, held by this server
// process. The backend can stall for tens of seconds (requests queue before
// a handler runs), and the factory board takes 20-35s even when it does not.
// Pages read from here instead of waiting on the backend: a read returns the
// last good value at once and refreshes it in the background, one read per
// key at a time and at most once per `freshMs`. Only a cold key (nothing held
// yet) waits, and only up to `coldWaitMs`, so a page always answers inside
// the gateway's 15s request timeout.
//
// Values are server-wide views (the board, the home aggregate, the task
// lists), never per-user data: every operator sees the same thing.

const MISSING = Symbol("missing");

async function readJson(fetchImpl, url, timeoutMs) {
  try {
    const res = await fetchImpl(url, {
      signal: AbortSignal.timeout(timeoutMs),
    });
    if (!res.ok) return MISSING;
    const body = await res.json();
    return body && typeof body === "object" && !body.error ? body : MISSING;
  } catch {
    return MISSING;
  }
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export function createBackendCache({
  fetchImpl = (...args) => globalThis.fetch(...args),
  now = Date.now,
} = {}) {
  const entries = new Map();

  function entry(key) {
    if (!entries.has(key))
      entries.set(key, {
        value: MISSING,
        at: -Infinity,
        attemptedAt: -Infinity,
        pending: null,
        forced: false,
      });
    return entries.get(key);
  }

  function refresh(key, url, timeoutMs) {
    const e = entry(key);
    if (e.pending) return e.pending;
    e.attemptedAt = now();
    e.forced = false;
    e.pending = readJson(fetchImpl, url, timeoutMs)
      .then((value) => {
        if (value !== MISSING) {
          e.value = value;
          e.at = now();
        }
      })
      // Nobody awaits a background read: it must never reject.
      .catch(() => {})
      .finally(() => {
        e.pending = null;
      });
    return e.pending;
  }

  /**
   * The held value for `key` (read from `url`), or null when there is none
   * younger than `staleMs`. Starts a refresh when the value is `freshMs` old
   * and the last attempt is too, or after `invalidate`.
   */
  async function read(
    key,
    url,
    {
      freshMs = 60000,
      staleMs = 600000,
      timeoutMs = 60000,
      coldWaitMs = 0,
    } = {},
  ) {
    const e = entry(key);
    const age = () => (e.value === MISSING ? Infinity : now() - e.at);
    if (e.forced || (age() >= freshMs && now() - e.attemptedAt >= freshMs))
      refresh(key, url, timeoutMs);
    if (age() >= staleMs && e.pending && coldWaitMs > 0)
      await Promise.race([e.pending, sleep(coldWaitMs)]);
    return age() < staleMs ? e.value : null;
  }

  /** The next read of `key` refreshes it; the held value is still served. */
  function invalidate(key) {
    entry(key).forced = true;
  }

  /**
   * How old the held value of `key` is in ms (null when none is held), and
   * whether a read of it is in flight.
   */
  function status(key) {
    const e = entry(key);
    return {
      ageMs: e.value === MISSING ? null : Math.max(0, now() - e.at),
      pending: e.pending != null,
    };
  }

  return { read, invalidate, status };
}

/** The process-wide cache the private pages share. */
export const backendCache = createBackendCache();

/**
 * The full factory board (`/api/agents/factory`, task-less). Fresh for a
 * minute: the backend does one board read a minute per process. A reading
 * is kept for half an hour, so a page opened after a quiet spell shows the
 * last board (with its age) rather than none; the page says how old it is.
 */
export const BOARD_STALE_MS = 30 * 60_000;

export function readBoard(
  apiBase,
  { coldWaitMs = 0, cache = backendCache } = {},
) {
  return cache.read("board", `${apiBase}/api/agents/factory`, {
    freshMs: 60000,
    staleMs: BOARD_STALE_MS,
    timeoutMs: 90000,
    coldWaitMs,
  });
}

/** `{ ageMs, pending }` for the held board (see `status`). */
export function boardStatus(cache = backendCache) {
  return cache.status("board");
}

export function invalidateBoard(cache = backendCache) {
  cache.invalidate("board");
}

export function readHome(
  apiBase,
  { coldWaitMs = 8000, cache = backendCache } = {},
) {
  return cache.read("home", `${apiBase}/api/home/dashboard`, {
    freshMs: 60000,
    staleMs: 600000,
    timeoutMs: 60000,
    coldWaitMs,
  });
}

/** `which` is "daily" or "weekly". Fresh for 30s; a toggle invalidates both. */
export function readTasks(
  apiBase,
  which,
  { coldWaitMs = 8000, cache = backendCache } = {},
) {
  return cache.read(
    `tasks:${which}`,
    `${apiBase}/api/knowledge/tasks/${which}`,
    {
      freshMs: 30000,
      staleMs: 600000,
      timeoutMs: 60000,
      coldWaitMs,
    },
  );
}

export function invalidateTasks(cache = backendCache) {
  cache.invalidate("tasks:daily");
  cache.invalidate("tasks:weekly");
}

/**
 * Keep the private root's reads warm in this process, so the first visit
 * after a deploy or a quiet spell answers with a reading, not a wait. Each
 * tick only refreshes what is past its freshness window. Opt-in: only the
 * private frontend sets PRIVATE_DASHBOARD_PREWARM.
 */
export function startPrivatePrewarm({
  apiBase = process.env.API_BASE,
  enabled = process.env.PRIVATE_DASHBOARD_PREWARM === "true",
  everyMs = 120_000,
  cache = backendCache,
} = {}) {
  if (!enabled || !apiBase) return null;
  const tick = () => {
    void readBoard(apiBase, { cache });
    void readHome(apiBase, { coldWaitMs: 0, cache });
  };
  tick();
  const timer = setInterval(tick, everyMs);
  timer.unref?.();
  return timer;
}
