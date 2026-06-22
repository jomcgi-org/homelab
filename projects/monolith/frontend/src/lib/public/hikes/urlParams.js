// URL <-> filter-state mapping for the hikes planner, so a shared link restores
// the day / region / numeric filters. Kept as a pure module (no Svelte, no
// $app imports) so the round-trip is unit-testable in the bare node env.
//
// State shape:
//   { selectedDay: string|null, selectedWalk: string|null, nearKey: string,
//     minDuration, maxDuration, minDistance, maxDistance, maxAscent: string }
// The numeric fields are kept as the same strings the <input> binds to ("" =
// no constraint), so init and write stay loss-free. selectedWalk is the open
// route card's uuid (deep-linkable), or null when no card is open.

// Param names: short and stable. Numeric filters are stored verbatim.
const NUM_PARAMS = {
  minDuration: "dmin",
  maxDuration: "dmax",
  minDistance: "kmin",
  maxDistance: "kmax",
  maxAscent: "ascent",
};

// A YYYY-MM-DD day key, the only `day` shape we mint (validated loosely: the
// day strip rejects keys that match no walk anyway, so this just guards junk).
const DAY_RE = /^\d{4}-\d{2}-\d{2}$/;

// A walk uuid (uuid5 of the coordinates). Validated to the canonical 8-4-4-4-12
// hex shape so a junk `walk` param is ignored; an unknown-but-well-formed uuid
// just opens no card (the map has no such marker).
const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

// A finite, non-negative number (matches the <input type=number min=0> domain).
function parseNum(raw) {
  if (raw == null || raw === "") return "";
  const n = Number(raw);
  return Number.isFinite(n) && n >= 0 ? raw : "";
}

// Read the filter state out of a URLSearchParams, falling back to defaults for
// absent or invalid params. `validNearKeys` is the allow-list of region keys
// (plus the geo sentinel) so a stale/garbage `near` is ignored.
export function readHikeParams(searchParams, validNearKeys) {
  const rawDay = searchParams.get("day");
  const rawWalk = searchParams.get("walk");
  const rawNear = searchParams.get("near");
  return {
    selectedDay: rawDay && DAY_RE.test(rawDay) ? rawDay : null,
    selectedWalk: rawWalk && UUID_RE.test(rawWalk) ? rawWalk : null,
    nearKey: rawNear && validNearKeys.has(rawNear) ? rawNear : "",
    minDuration: parseNum(searchParams.get(NUM_PARAMS.minDuration)),
    maxDuration: parseNum(searchParams.get(NUM_PARAMS.maxDuration)),
    minDistance: parseNum(searchParams.get(NUM_PARAMS.minDistance)),
    maxDistance: parseNum(searchParams.get(NUM_PARAMS.maxDistance)),
    maxAscent: parseNum(searchParams.get(NUM_PARAMS.maxAscent)),
  };
}

// Mirror the filter state onto a copy of `url`'s params, deleting anything at
// its default (null day, "" near, "" numeric) so shared links stay clean.
// Returns the mutated URLSearchParams (caller decides whether to goto).
export function writeHikeParams(searchParams, state) {
  const set = (key, value) => {
    if (value) searchParams.set(key, value);
    else searchParams.delete(key);
  };
  set("day", state.selectedDay);
  set("walk", state.selectedWalk);
  set("near", state.nearKey);
  for (const [field, key] of Object.entries(NUM_PARAMS)) {
    set(key, state[field]);
  }
  return searchParams;
}
