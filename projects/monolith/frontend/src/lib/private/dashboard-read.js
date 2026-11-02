// Pure helpers for the private root's refresh and readings. The page loads
// one merged read (dashboard-load.js) and refreshes it every 60 seconds;
// these decide what a refresh replaces and how a reading is worded.

/** The sections a dashboard read carries, each from one backend source. */
export const SECTIONS = ["today", "health", "github", "factory"];

/**
 * Refreshes a section may miss before its last good reading is dropped.
 * The page spaces refreshes at least 30 seconds apart (60 on its timer), so
 * one miss is a blip and three are a source down for a minute or more.
 */
export const STALE_AFTER = 3;

/**
 * Merge a refresh into the shown read. A section the refresh brings
 * replaces the old one and clears its miss count. A section it lacks keeps
 * the last good reading until it has missed STALE_AFTER refreshes in a
 * row, then reads as missing. A failed refresh (no read at all, or one that
 * reports {error}) is a miss for every section, so a dead backend reads as
 * dead too. Returns `{ read, misses }`; neither input is mutated.
 */
export function mergeRead(previous, next, misses = {}) {
  if (!next || typeof next !== "object" || next.error) next = {};
  if (previous == null) {
    return Object.keys(next).length
      ? mergeRead({}, next, misses)
      : { read: previous, misses };
  }
  const read = { ...previous };
  const counted = { ...misses };
  for (const key of SECTIONS) {
    const value = next[key];
    if (value != null && !value.error) {
      read[key] = value;
      counted[key] = 0;
    } else if (read[key] != null) {
      counted[key] = (counted[key] ?? 0) + 1;
      if (counted[key] >= STALE_AFTER) read[key] = null;
    }
  }
  return { read, misses: counted };
}

/**
 * One CI vocabulary for the page. The home aggregate reduces check runs to
 * passing/failing/pending (home/dashboard.py); success/failure are GitHub's
 * own conclusion words and are accepted too.
 */
export function ciState(ci) {
  if (ci === "passing" || ci === "success") return "ok";
  if (ci === "failing" || ci === "failure") return "bad";
  return "pending";
}

/**
 * The readings a read could not supply, by the name the page uses for
 * them. An empty Needs you list only means "nothing waiting" when every
 * reading answered.
 */
export function missingReadings(read) {
  if (read == null) return ["cluster", "factory", "github"];
  const missing = [];
  if (read.health == null || read.health.error) missing.push("cluster");
  // A board that answered "not ready" cannot say what is waiting either.
  if (read.factory == null || read.factory.ok === false)
    missing.push("factory");
  if (read.github == null || read.github.error) missing.push("github");
  return missing;
}
