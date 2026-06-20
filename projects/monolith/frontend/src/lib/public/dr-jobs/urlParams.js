// URL <-> view-state mapping for the dr-jobs board, so a shared link restores
// the live/history view and the town filter. Pure module (no Svelte, no $app
// imports) so the round-trip is unit-testable in the bare node env.
//
// State shape: { view: "live"|"history", town: string }
// `town` is a free string (the set of valid towns is data-dependent): an
// unknown town is left in place here and the page's auto-reset clears it when
// it is absent from the active bucket, which then mirrors "" back to the URL.

const VIEWS = new Set(["live", "history"]);

// Read the view state out of a URLSearchParams. An invalid view falls back to
// the "live" default (mirroring the server-side review enum validation).
export function readDrJobsParams(searchParams) {
  const rawView = searchParams.get("view");
  return {
    view: rawView && VIEWS.has(rawView) ? rawView : "live",
    town: searchParams.get("town") ?? "",
  };
}

// Mirror the view state onto `searchParams`, deleting anything at its default
// (view "live", empty town) so shared links stay clean.
export function writeDrJobsParams(searchParams, state) {
  if (state.view === "history") searchParams.set("view", "history");
  else searchParams.delete("view"); // live is the default

  if (state.town) searchParams.set("town", state.town);
  else searchParams.delete("town");

  return searchParams;
}
