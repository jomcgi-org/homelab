// Pure, testable helpers for public-chat view state (ADR 005, Phase 4 polish).
//
// The Svelte page keeps each piece of chat state in its own `$state` rune, but
// the SHAPE of a fresh conversation (an empty transcript, an empty grounding
// set, an idle turn, no notice) lives here so it is unit-testable and so the
// initial render and the NEW CHAT reset cannot drift apart.

import { initialTurnState } from "./stream.js";

/**
 * The state of a brand-new conversation: empty transcript, empty grounding set,
 * idle turn, no notice, empty input. Used for both the first render and the
 * NEW CHAT reset so the two can never diverge.
 *
 * @returns {{ messages: any[], touchedMap: Map<any, any>, turn: ReturnType<typeof initialTurnState>, notice: null, input: string, lastUserMessage: string }}
 */
export function freshChatState() {
  return {
    messages: [],
    touchedMap: new Map(),
    turn: initialTurnState(),
    notice: null,
    input: "",
    lastUserMessage: "",
  };
}

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
 * Map an optional focus id (a "grounded in" chip click on the chat side) to a
 * panel selection. A null/undefined focus id selects nothing, so opening the
 * graph from the GRAPH tab or DEEP DIVE leaves the panel empty.
 *
 * @param {any} focusId
 * @returns {any}
 */
export function selectionForFocus(focusId) {
  return focusId == null ? null : focusId;
}
