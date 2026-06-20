// URL <-> view-state mapping for the stargazing map, so a shared link restores
// the mode and the time selection. Pure module (no Svelte, no $app imports) so
// the round-trip is unit-testable in the bare node env.
//
// State shape: { mode: "live"|"historical", selectedNight: "all"|YYYY-MM-DD,
//                selectedMonth: 0..12 }
// Only the param relevant to the active mode is written: `night` in live,
// `month` in historical. Defaults (mode "live", night "all", month 0) are
// omitted so a fresh share URL is clean.

const MODES = new Set(["live", "historical"]);
const NIGHT_RE = /^\d{4}-\d{2}-\d{2}$/;
export const ALL_YEAR = 0;
const ALL_NIGHTS = "all";

// Read the view state out of a URLSearchParams, falling back to defaults for
// absent or invalid params (invalid enum -> default, mirroring the server-side
// review validation).
export function readStarsParams(searchParams) {
  const rawMode = searchParams.get("mode");
  const mode = rawMode && MODES.has(rawMode) ? rawMode : "live";

  const rawNight = searchParams.get("night");
  const selectedNight =
    rawNight && NIGHT_RE.test(rawNight) ? rawNight : ALL_NIGHTS;

  const rawMonth = Number(searchParams.get("month"));
  const selectedMonth =
    Number.isInteger(rawMonth) && rawMonth >= 1 && rawMonth <= 12
      ? rawMonth
      : ALL_YEAR;

  return { mode, selectedNight, selectedMonth };
}

// Mirror the view state onto `searchParams`, writing only the param for the
// active mode and deleting the other (so switching modes does not leave a
// stale `night`/`month` behind) plus anything at its default.
export function writeStarsParams(searchParams, state) {
  if (state.mode === "historical") {
    searchParams.set("mode", "historical");
  } else {
    searchParams.delete("mode"); // live is the default
  }

  if (state.mode === "live" && state.selectedNight !== ALL_NIGHTS) {
    searchParams.set("night", state.selectedNight);
  } else {
    searchParams.delete("night");
  }

  if (state.mode === "historical" && state.selectedMonth !== ALL_YEAR) {
    searchParams.set("month", String(state.selectedMonth));
  } else {
    searchParams.delete("month");
  }

  return searchParams;
}
