// Pure, DOM-free session-constellation state for the chat page's grounded
// graph panel. Nodes come only from `node_touched` frames with kind
// "entity" (chunks never appear in the graph); edges are never fabricated,
// they only ever come from a best-effort `exploreEgo(id)` fetch, and only
// survive into `edges` when BOTH endpoints are already session nodes.
//
// Egos are kept (keyed by the id they were fetched for) rather than only
// their edges, and `withEgo` recomputes the full edge list from every stored
// ego on each call. That makes edge derivation order-independent: if node B
// arrives after node A's ego already named a B->A relationship, the next
// `withEgo` call (A's or B's) picks it up, instead of the relationship being
// silently dropped because A's ego happened to resolve before B existed.

export function emptyConstellation() {
  return { nodes: [], ids: new Set(), edges: [], egos: new Map() };
}

// Fold one touched item into the state. Only new, kind==="entity" items add
// a node; everything else (chunks, duplicates) is a no-op that returns the
// same state reference so callers can cheaply skip a Svelte reassignment.
export function withTouched(state, item) {
  if (!item || item.kind !== "entity") return state;
  const id = item.id;
  if (id === undefined || id === null || state.ids.has(id)) return state;
  const node = { id, name: item.title ?? "", entity_type: item.entity_type };
  const ids = new Set(state.ids);
  ids.add(id);
  return { ...state, nodes: [...state.nodes, node], ids };
}

// Record the ego response fetched for `forId` and recompute edges from
// every ego on file. Malformed/empty ego payloads are tolerated (treated as
// no edges for that id).
export function withEgo(state, forId, ego) {
  const egos = new Map(state.egos);
  egos.set(forId, ego && Array.isArray(ego.edges) ? ego.edges : []);
  const seen = new Set();
  const edges = [];
  for (const rawEdges of egos.values()) {
    for (const e of rawEdges) {
      if (!e || !state.ids.has(e.from) || !state.ids.has(e.to)) continue;
      if (e.from === e.to) continue;
      // Normalize to strings before ordering so the pair key is stable
      // regardless of whether ids arrive as numbers or strings.
      const a = String(e.from);
      const b = String(e.to);
      const key = a < b ? a + "|" + b : b + "|" + a;
      if (seen.has(key)) continue;
      seen.add(key);
      edges.push({ from: e.from, to: e.to });
    }
  }
  return { ...state, egos, edges };
}
