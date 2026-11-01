import { createSseParser } from "$lib/public/sse-parser.js";

export { createSseParser };

export const MOVING_CHAT_PATH = "/api/moving/chat";
export const CHARACTER_LIMIT = 2000;

const FALLBACK_ERROR = "Something went wrong. Please try again.";

export function initialTurnState() {
  return { status: "idle", assistant: "", error: "" };
}

export function applyFrame(state, frame) {
  switch (frame?.type) {
    case "token":
      return {
        ...state,
        status: "streaming",
        assistant: state.assistant + (frame.data?.text ?? ""),
      };
    case "done":
      return { ...state, status: "done" };
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

function messageForError(status, detail) {
  if (detail && typeof detail.message === "string" && detail.message) {
    return detail.message;
  }
  if (typeof detail === "string" && detail) return detail;
  if (status === 422) {
    return `Keep each message under ${CHARACTER_LIMIT} characters and try again.`;
  }
  if (status === 503) return "Chat is unavailable right now. Please try again.";
  return FALLBACK_ERROR;
}

export async function streamChatMessage(
  message,
  history,
  { fetchImpl = fetch, signal, onFrame } = {},
) {
  const response = await fetchImpl(MOVING_CHAT_PATH, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ message, history }),
    signal,
  });

  if (!response.ok) {
    let body = null;
    try {
      body = await response.json();
    } catch {
      body = null;
    }
    const detail = body?.detail ?? body ?? {};
    onFrame?.({
      type: "error",
      data: {
        code: "error",
        message: messageForError(response.status, detail),
      },
    });
    return;
  }

  const reader = response.body?.getReader?.();
  if (!reader) {
    onFrame?.({ type: "error", data: { message: FALLBACK_ERROR } });
    return;
  }
  const decoder = new TextDecoder();
  const parser = createSseParser();
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    for (const frame of parser.push(decoder.decode(value, { stream: true }))) {
      onFrame?.(frame);
    }
  }
  for (const frame of parser.flush()) onFrame?.(frame);
}
