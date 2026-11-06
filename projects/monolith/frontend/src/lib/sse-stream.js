import { createSseParser } from "./public/sse-parser.js";

/**
 * Consume a response body as JSON SSE. Callers own HTTP and dispatch policy.
 * Returning false from onFrame stops consumption. Callback failures propagate.
 * Aborts reject with the signal reason and suppress remaining buffered frames.
 */
export async function consumeSseStream(body, onFrame, { signal } = {}) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  const parser = createSseParser();
  let completed = false;
  let cancellation;
  function cancel() {
    cancellation ??= reader.cancel(signal?.reason).catch(() => {});
  }
  function dispatch(frames) {
    for (const frame of frames) {
      signal?.throwIfAborted();
      const stop = onFrame?.(frame) === false;
      signal?.throwIfAborted();
      if (stop) return false;
    }
    return true;
  }
  signal?.addEventListener("abort", cancel, { once: true });
  try {
    signal?.throwIfAborted();
    for (;;) {
      const { value, done } = await reader.read();
      signal?.throwIfAborted();
      if (done) break;
      if (!dispatch(parser.push(decoder.decode(value, { stream: true }))))
        return;
    }
    if (!dispatch(parser.push(decoder.decode()))) return;
    if (!dispatch(parser.flush())) return;
    completed = true;
  } finally {
    signal?.removeEventListener("abort", cancel);
    if (!completed) cancel();
    try {
      await cancellation;
    } finally {
      reader.releaseLock();
    }
  }
}
