// Pure, testable helper for the Grimoire chat view state. Mirrors
// lib/public/chat/chat-state.js (the notes chat seam), minus the
// graph-selection helpers: the Grimoire chat has no graph view to focus, so
// only the fresh-conversation shape is needed here.
//
// The Svelte page keeps each piece of chat state in its own `$state` rune, but
// the SHAPE of a fresh conversation (an empty transcript, an empty grounding
// set, an idle turn, no notice) lives here so it is unit-testable and so the
// initial render and the NEW CHAT reset cannot drift apart.

import { initialTurnState } from "./stream.js";

/**
 * The state of a brand-new conversation: empty transcript, empty grounding
 * set, idle turn, no notice, empty input. Used for both the first render and
 * the NEW CHAT reset so the two can never diverge.
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
