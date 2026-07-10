// Session-scoped Svelte store wrapping constellation-state.js's pure folds
// with sessionStorage persistence, so the "trail of your curiosity" survives
// a reload/navigation within the same tab but never leaks across tabs or
// sessions (sessionStorage, not localStorage). One store instance is shared
// module-wide: every grimoire page imports the same singleton, so touches
// from chat grounding, World card opens, and reader mention taps (later
// tasks) all accrue into the same graph regardless of which page fired them.
//
// Persistence shape on disk is plain-JSON-able (nodes/edges arrays, ids as an
// array, egos as [id, edges][] entries) since Set/Map do not survive
// JSON.stringify; hydrate() reconstructs the Set/Map that
// constellation-state.js's functions expect.

import {
  emptyConstellation,
  withTouched,
  withEgo,
} from "./constellation-state.js";

const STORAGE_KEY = "grimoire.constellation";

function hasSessionStorage() {
  return typeof sessionStorage !== "undefined";
}

function serialize(state) {
  return JSON.stringify({
    nodes: state.nodes,
    ids: [...state.ids],
    edges: state.edges,
    egos: [...state.egos.entries()],
  });
}

// Tolerates a missing key, corrupt JSON, or a payload shaped unlike what we
// wrote (e.g. from an older version) by falling back to an empty state.
function deserialize(raw) {
  if (!raw) return emptyConstellation();
  try {
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return emptyConstellation();
    const nodes = Array.isArray(parsed.nodes) ? parsed.nodes : [];
    const ids = new Set(Array.isArray(parsed.ids) ? parsed.ids : []);
    const edges = Array.isArray(parsed.edges) ? parsed.edges : [];
    const egos = new Map(Array.isArray(parsed.egos) ? parsed.egos : []);
    return { nodes, ids, edges, egos };
  } catch {
    return emptyConstellation();
  }
}

function loadInitial() {
  if (!hasSessionStorage()) return emptyConstellation();
  try {
    return deserialize(sessionStorage.getItem(STORAGE_KEY));
  } catch {
    // sessionStorage can throw (private-browsing quota, disabled storage):
    // degrade to in-memory rather than breaking the page.
    return emptyConstellation();
  }
}

function persist(state) {
  if (!hasSessionStorage()) return;
  try {
    sessionStorage.setItem(STORAGE_KEY, serialize(state));
  } catch {
    // Quota exceeded or storage disabled: the in-memory store still works,
    // it just will not survive a reload. Not worth surfacing to the user.
  }
}

// Builds a fresh store instance. Exported mainly so tests can construct
// isolated instances against a mock sessionStorage; app code should import
// the shared `constellationStore` singleton below instead.
export function createConstellationStore() {
  let state = loadInitial();
  const subscribers = new Set();

  function notify() {
    persist(state);
    for (const fn of subscribers) fn(state);
  }

  function subscribe(fn) {
    fn(state);
    subscribers.add(fn);
    return () => subscribers.delete(fn);
  }

  // Fold one touched item (an entity, chunk, etc. from grounding, a World
  // card open, or a reader mention tap) into the constellation. Only
  // kind === "entity" items add a node (constellation-state.js's contract);
  // everything else is a no-op.
  function touch(item) {
    const next = withTouched(state, item);
    if (next === state) return;
    state = next;
    notify();
  }

  // Record an ego fetch's result for `forId` and recompute edges. Callers
  // fire this after a best-effort exploreEgo(id) resolves for a newly
  // touched entity.
  function recordEgo(forId, ego) {
    state = withEgo(state, forId, ego);
    notify();
  }

  function clear() {
    state = emptyConstellation();
    notify();
  }

  return { subscribe, touch, recordEgo, clear };
}

export const constellationStore = createConstellationStore();
