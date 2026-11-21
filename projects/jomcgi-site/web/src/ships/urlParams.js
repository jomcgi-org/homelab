// URL <-> view-state mapping for the ships map, so a shared link restores the
// vessel-type filter, the vessels/heat mode, and the selected vessel. Pure
// module (no Svelte, no $app imports) so the round-trip is unit-testable in the
// bare node env.
//
// State shape: { active: Set<string>, mode: "vessels"|"heat", mmsi: string|null }
// `active` is the set of enabled vessel-type keys; the default is "all on", so
// `types` is written only when some are off (and omitted when all are active).

const MODES = new Set(["vessels", "heat"]);

// Read the view state out of a URLSearchParams. `allKeys` is the full ordered
// list of vessel-type keys (the legend), used both as the allow-list for the
// `types` param and as the "all on" default when it is absent. Unknown type
// tokens are dropped; if that leaves an empty set, fall back to all-on (an
// empty filter would hide every vessel, which is never a useful shared state).
export function readShipsParams(searchParams, allKeys) {
  const allSet = new Set(allKeys);

  const rawTypes = searchParams.get("types");
  let active;
  if (rawTypes == null) {
    active = new Set(allKeys); // default: everything visible
  } else {
    const picked = rawTypes
      .split(",")
      .map((t) => t.trim())
      .filter((t) => allSet.has(t));
    active = picked.length ? new Set(picked) : new Set(allKeys);
  }

  const rawMode = searchParams.get("mode");
  const mode = rawMode && MODES.has(rawMode) ? rawMode : "vessels";

  const rawMmsi = searchParams.get("mmsi");
  // MMSI is a 9-digit maritime identity; accept only digit strings, ignore junk.
  const mmsi = rawMmsi && /^\d+$/.test(rawMmsi) ? rawMmsi : null;

  return { active, mode, mmsi };
}

// Mirror the view state onto `searchParams`, deleting anything at its default
// (all types active, vessels mode, no selection) so shared links stay clean.
// `allKeys` is the full legend order, so `types` is written in a stable order.
export function writeShipsParams(searchParams, state, allKeys) {
  if (state.active.size === allKeys.length) {
    searchParams.delete("types"); // all on: the default
  } else {
    const ordered = allKeys.filter((k) => state.active.has(k));
    searchParams.set("types", ordered.join(","));
  }

  if (state.mode === "heat") searchParams.set("mode", "heat");
  else searchParams.delete("mode"); // vessels is the default

  if (state.mmsi) searchParams.set("mmsi", String(state.mmsi));
  else searchParams.delete("mmsi");

  return searchParams;
}
