export const MOVING_CHAT_PATH = "/api/moving/chat";
export const CHARACTER_LIMIT = 2000;

const FALLBACK_ERROR = "Something went wrong. Please try again.";

export function createSseParser() {
  let buffer = "";

  function parseBlock(block) {
    const dataLines = [];
    for (const raw of block.split("\n")) {
      const line = raw.replace(/\r$/, "");
      if (line.startsWith("data:")) {
        dataLines.push(line.slice(5).replace(/^ /, ""));
      }
    }
    if (dataLines.length === 0) return null;
    try {
      return JSON.parse(dataLines.join("\n"));
    } catch {
      return null;
    }
  }

  return {
    push(chunk) {
      buffer += chunk.replace(/\r\n/g, "\n");
      const frames = [];
      let index;
      while ((index = buffer.indexOf("\n\n")) !== -1) {
        const block = buffer.slice(0, index);
        buffer = buffer.slice(index + 2);
        const frame = parseBlock(block);
        if (frame) frames.push(frame);
      }
      return frames;
    },
    flush() {
      const block = buffer.trim();
      buffer = "";
      if (!block) return [];
      const frame = parseBlock(block);
      return frame ? [frame] : [];
    },
  };
}

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
