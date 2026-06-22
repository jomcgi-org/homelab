// Module-level cache of the parsed public knowledge graph.
//
// GraphView remounts every time the visitor toggles Chat <-> Graph, and its
// onMount re-fetched and re-parsed the full ~3.4 MB graph on each toggle. A
// module-level singleton persists across those remounts within one page
// session, so the second and later opens are instant: no fetch, no JSON parse,
// no rebuild. A full page reload starts a fresh module and clears it, which is
// the correct TTL -- cross-load freshness is already governed by the HTTP layer
// (s-maxage / ETag on /app/notes/graph).
let cache = null;

export function getCachedGraph() {
  return cache;
}

export function setCachedGraph(graph) {
  cache = graph;
}
