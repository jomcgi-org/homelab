import { describe, expect, it, vi } from "vitest";
import { consumeSseStream } from "./sse-stream.js";

const encoder = new TextEncoder();
function bodyFrom(chunks, hooks = {}) {
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(chunk);
      if (!hooks.open) controller.close();
      hooks.controller = controller;
    },
    cancel: hooks.cancel,
  });
}
const event = (id) => ({ type: "token", data: { text: `雪🙂${id}` } });
const frame = (value, ending = "\n", space = " ") =>
  `data:${space}${JSON.stringify(value)}${ending}${ending}`;

describe("synthetic SSE response consumption", () => {
  for (const ending of ["\n", "\r\n"]) {
    for (const space of ["", " "]) {
      const expected = [event(1), event(2), { type: "done" }];
      const bytes = encoder.encode(
        `: comment${ending}${ending}` +
          frame(expected[0], ending, space) +
          `data: broken${ending}${ending}` +
          `data:${space}{"type":${ending}data:${space}"token","data":{"text":"雪🙂2"}}${ending}${ending}` +
          // Preserve the repository convention of flushing trailing EOF data.
          'data: {"type":"done"}',
      );
      it(`exactly-once ordered delivery, every byte split: ${JSON.stringify({ ending, space })}`, async () => {
        const partitions = Array.from({ length: bytes.length + 1 }, (_, i) => [
          bytes.slice(0, i),
          bytes.slice(i),
        ]);
        partitions.push(Array.from(bytes, (byte) => Uint8Array.of(byte)));
        for (const widths of [
          [1, 2, 7, 3],
          [13, 1, 1, 2, 5],
        ]) {
          const chunks = [];
          for (let offset = 0, i = 0; offset < bytes.length; i++) {
            const width = widths[i % widths.length];
            chunks.push(bytes.slice(offset, offset + width));
            offset += width;
          }
          partitions.push(chunks);
        }
        for (const chunks of partitions) {
          const body = bodyFrom(chunks);
          const delivered = [];
          await consumeSseStream(body, (value) => {
            delivered.push(value);
          });
          expect(delivered).toEqual(expected);
          expect(body.locked).toBe(false);
        }
      });
    }
  }

  it("handles the mixed CRLF/LF terminator reproduction", async () => {
    const expected = [
      { type: "text_chunk", data: { text: "hello" } },
      { type: "done" },
    ];
    const body = bodyFrom([
      encoder.encode(`data: ${JSON.stringify(expected[0])}\r\n\r`),
      encoder.encode(`\ndata: ${JSON.stringify(expected[1])}\n\n`),
    ]);
    const delivered = [];
    await consumeSseStream(body, (value) => {
      delivered.push(value);
    });
    expect(delivered).toEqual(expected);
    expect(body.locked).toBe(false);
  });

  it("finalizes an incomplete UTF-8 sequence at EOF", async () => {
    // The replacement character must reach parser.flush, even though this
    // incomplete JSON is discarded. Observe final decode without mocking it.
    const decode = vi.spyOn(TextDecoder.prototype, "decode");
    const body = bodyFrom([
      encoder.encode('data: {"text":"'),
      Uint8Array.of(0xe9),
    ]);
    const onFrame = vi.fn();
    try {
      await consumeSseStream(body, onFrame);
      expect(decode).toHaveBeenLastCalledWith();
      expect(decode.mock.results.at(-1).value).toBe("�");
      expect(onFrame).not.toHaveBeenCalled();
      expect(body.locked).toBe(false);
    } finally {
      decode.mockRestore();
    }
  });

  it("releases the reader after read failure", async () => {
    const failure = new Error("read failed");
    const body = new ReadableStream({
      pull(controller) {
        controller.error(failure);
      },
    });
    await expect(consumeSseStream(body, vi.fn())).rejects.toBe(failure);
    expect(body.locked).toBe(false);
  });

  for (const mode of ["throw", "stop", "abort"]) {
    it(`${mode} suppresses later buffered frames and cancels before unlocking`, async () => {
      const abort = new AbortController();
      const failure = new Error("callback failed");
      let finishCancel;
      const cancel = vi.fn(
        () =>
          new Promise((resolve) => {
            finishCancel = resolve;
          }),
      );
      const body = bodyFrom(
        [encoder.encode(frame(event(1)) + frame(event(2)))],
        { open: true, cancel },
      );
      const delivered = [];
      const pending = consumeSseStream(
        body,
        (value) => {
          delivered.push(value);
          if (mode === "throw") throw failure;
          if (mode === "abort") abort.abort(failure);
          if (mode === "stop") return false;
        },
        { signal: abort.signal },
      );
      // Attach a rejection handler before letting the async operation settle.
      const outcome = pending.then(
        () => null,
        (error) => error,
      );
      await vi.waitFor(() => expect(cancel).toHaveBeenCalledTimes(1));
      expect(body.locked).toBe(true);
      finishCancel();
      expect(await outcome).toBe(mode === "stop" ? null : failure);
      expect(delivered).toEqual([event(1)]);
      expect(body.locked).toBe(false);
    });
  }

  for (const preAborted of [false, true]) {
    it(`cancels ${preAborted ? "before reading" : "a pending read"} and removes the abort listener`, async () => {
      const abort = new AbortController();
      const reason = new Error("aborted");
      const remove = vi.spyOn(abort.signal, "removeEventListener");
      const cancel = vi.fn();
      const body = bodyFrom([], { open: true, cancel });
      const delivered = vi.fn();
      if (preAborted) abort.abort(reason);
      const outcome = consumeSseStream(body, delivered, {
        signal: abort.signal,
      }).catch((error) => error);
      await Promise.resolve();
      if (!preAborted) abort.abort(reason);
      expect(await outcome).toBe(reason);
      expect(cancel).toHaveBeenCalledTimes(1);
      expect(delivered).not.toHaveBeenCalled();
      expect(body.locked).toBe(false);
      expect(remove).toHaveBeenCalledWith("abort", expect.any(Function));
    });
  }

  it("releases the lock even when cancellation rejects", async () => {
    const failure = new Error("dispatch failed");
    const body = bodyFrom([encoder.encode(frame(event(1)))], {
      open: true,
      cancel: () => Promise.reject(new Error("cancel failed")),
    });
    await expect(
      consumeSseStream(body, () => {
        throw failure;
      }),
    ).rejects.toBe(failure);
    expect(body.locked).toBe(false);
  });
});
