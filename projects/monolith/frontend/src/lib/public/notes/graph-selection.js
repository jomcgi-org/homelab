// Pure, testable helpers for the public notes graph view's selection state.
//
// These began life as part of the notes chat's view-state helpers (ADR 005,
// Phase 4 polish). The chat is retired (#6913); the graph view and these
// helpers survive because /app/notes still renders the public knowledge graph
// and the factory context viewer deep-links into it with ?focus=<note id>.

/**
 * The graph view never auto-selects a node: a fresh graph has no selection and
 * the detail panel shows its placeholder until the visitor hovers or clicks.
 *
 * @returns {null}
 */
export function initialGraphSelection() {
  return null;
}

/**
 * Map an optional focus id (a ?focus= query parameter from a deep link) to a
 * panel selection. A null/undefined focus id selects nothing, so opening the
 * graph without a focus leaves the panel empty.
 *
 * @param {any} focusId
 * @returns {any}
 */
export function selectionForFocus(focusId) {
  return focusId == null ? null : focusId;
}
