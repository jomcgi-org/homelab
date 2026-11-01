// Client-side Grimoire-chat turn streaming (ADR 005 pattern, grimoire_chat
// backend). Began as a copy of the retired notes chat's stream seam; only the
// proxy path differed.
//
// The browser never talks to the internal chat API: it POSTs the single user
// message to the same-origin SSR proxy (/app/grimoire/chat/message), which
// forwards it (session id comes from the httpOnly cookie, never the body) and
// passes the upstream SSE stream straight back. This module is the single,
// testable seam that POSTs the message, parses the SSE frames, and reduces
// them into the chat view state, so the Svelte component stays a thin shell
// around it.
//
// Wire format (one frame per blank-line-terminated block, single `data:` line):
//   data: {"type":"node_touched","data":{"id":<id>,"title":<title>,"kind":<"chunk"|"entity">,"entity_type"?,"book_id"?,"chunk_ref"?}}
//   data: {"type":"token","data":{"text":<delta>}}
//   data: {"type":"done","data":{"turn_count":<n>,"total_tokens":<n>}}
//   data: {"type":"busy","data":{"code":"busy","message":<text>}}
//   data: {"type":"error","data":{"code":"error","message":<text>}}

import { createSseParser } from "../../sse-parser.js";

export { createSseParser };

// Same-origin SSR proxy path. This route lives directly under
// routes/public/app/grimoire/chat/message, so no reroute-hook mapping is
// needed (unlike the top-level /chat/* paths the retired notes surface used).
export const MESSAGE_PROXY_PATH = "/app/grimoire/chat/message";

// Mirrors the backend CHAT_PUBLIC_CHAR_CAP default (grimoire_chat/limits.py).
// The server is authoritative;
// this is only a courtesy ceiling on the textarea so a user does not compose
// a message the backend will reject with a 400 char_cap.
export const CHARACTER_LIMIT = 8000;

// Default user-facing copy when the backend does not supply a message. No
// em-dashes anywhere (CLAUDE.md).
const FALLBACK_BUSY =
  "The grimoire is busy right now. Give it a moment and try again.";
const FALLBACK_ERROR = "Something went wrong. Please try again.";

/**
 * The view state for one assistant turn.
 *
 * `status`: idle (no turn yet) | streaming (tokens arriving) | done | busy |
 * error. `touched` is the ordered, deduped set of Grimoire corpus passages
 * (chunks or entities) the turn grounded on, carrying the full node_touched
 * shape (id, title, kind, plus entity_type for an entity or book_id/chunk_ref
 * for a chunk) so a GROUNDED IN chip can deep-link. `assistant` is the
 * streamed reply.
 *
 * @returns {{ status: string, assistant: string, touched: {id: any, title: string, kind: string, entity_type?: string, book_id?: string, chunk_ref?: string}[], error: string, turnCount: number, totalTokens: number }}
 */
export function initialTurnState() {
  return {
    status: "idle",
    assistant: "",
    touched: [],
    error: "",
    turnCount: 0,
    totalTokens: 0,
  };
}

/**
 * Fold one SSE frame into the turn state, returning a new state object.
 *
 * Pure: no DOM, no I/O. The `node_touched` reducer dedupes by id and
 * preserves arrival order.
 *
 * @param {ReturnType<typeof initialTurnState>} state
 * @param {object} frame
 */
export function applyFrame(state, frame) {
  switch (frame?.type) {
    case "node_touched": {
      const id = frame.data?.id;
      if (id === undefined || id === null) return state;
      if (state.touched.some((n) => n.id === id)) return state;
      // Carry every field the backend sends (kind, plus entity_type or
      // book_id/chunk_ref) so a committed GROUNDED IN chip can deep-link;
      // undefined fields are dropped rather than kept as explicit keys.
      const { title, kind, entity_type, book_id, chunk_ref } = frame.data ?? {};
      const node = { id, title: title ?? "" };
      if (kind !== undefined) node.kind = kind;
      if (entity_type !== undefined) node.entity_type = entity_type;
      if (book_id !== undefined) node.book_id = book_id;
      if (chunk_ref !== undefined) node.chunk_ref = chunk_ref;
      return {
        ...state,
        touched: [...state.touched, node],
      };
    }
    case "token":
      return {
        ...state,
        status: "streaming",
        assistant: state.assistant + (frame.data?.text ?? ""),
      };
    case "done":
      return {
        ...state,
        status: "done",
        turnCount: frame.data?.turn_count ?? state.turnCount,
        totalTokens: frame.data?.total_tokens ?? state.totalTokens,
      };
    case "busy":
      return {
        ...state,
        status: "busy",
        error: frame.data?.message || FALLBACK_BUSY,
      };
    case "error":
      return {
        ...state,
        status: "error",
        error: frame.data?.message || FALLBACK_ERROR,
      };
    default:
      return state;
  }
}

// Map a pre-stream HTTP error (the proxy relays the backend status + body
// before any SSE starts) to a human message. The soft, retryable shed arrives
// in-stream as a 200 `busy` frame; these are the terminal pre-stream cases.
function messageForError(status) {
  switch (status) {
    case 400:
      return `That message is too long (max ${CHARACTER_LIMIT} characters). Please shorten it.`;
    case 404:
      return "Your chat session expired. Reload the page to start a new one.";
    case 429:
      return "This conversation has reached its length limit. Reload the page to start a new one.";
    default:
      return FALLBACK_ERROR;
  }
}

/**
 * POST a user message to the SSR chat proxy and deliver parsed SSE frames.
 *
 * A pre-stream HTTP error (no `ok`) is mapped to a single synthetic `error`
 * frame so the caller has exactly one code path (frames in, state out). The
 * session id is supplied by the httpOnly cookie at the proxy, never here.
 *
 * @param {string} message The single user message.
 * @param {{ fetchImpl?: typeof fetch, signal?: AbortSignal, onFrame?: (frame: object) => void }} [opts]
 */
export async function streamChatMessage(
  message,
  { fetchImpl = fetch, signal, onFrame } = {},
) {
  const resp = await fetchImpl(MESSAGE_PROXY_PATH, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ message }),
    signal,
  });

  if (!resp.ok) {
    let body = null;
    try {
      body = await resp.json();
    } catch {
      body = null;
    }
    const detail = body?.detail ?? body ?? {};
    onFrame?.({
      type: "error",
      data: {
        code: detail.code ?? "error",
        message: messageForError(resp.status),
      },
    });
    return;
  }

  const reader = resp.body?.getReader?.();
  if (!reader) return;
  const decoder = new TextDecoder();
  const parser = createSseParser();
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    const chunk = decoder.decode(value, { stream: true });
    for (const frame of parser.push(chunk)) onFrame?.(frame);
  }
  for (const frame of parser.flush()) onFrame?.(frame);
}
