import { describe, expect, it } from "vitest";
import { createSseParser } from "./sse-parser.js";

describe("SSE framing across chunks", () => {
  const events = [
    { type: "text_chunk", data: { text: "hello" } },
    { type: "done", data: {} },
  ];
  for (const ending of ["\n", "\r\n"]) {
    const wire = events
      .map((event) => `data: ${JSON.stringify(event)}${ending}${ending}`)
      .join("");
    it(`delivers exactly once at every ${JSON.stringify(ending)} split`, () => {
      for (let split = 0; split <= wire.length; split++) {
        const parser = createSseParser();
        expect(
          [
            ...parser.push(wire.slice(0, split)),
            ...parser.push(wire.slice(split)),
            ...parser.flush(),
          ],
          `split ${split}`,
        ).toEqual(events);
      }
    });
  }
  it("retains a split CRLF terminator before an LF-framed done", () => {
    const parser = createSseParser();
    expect([
      ...parser.push(`data: ${JSON.stringify(events[0])}\r\n\r`),
      ...parser.push(`\ndata: ${JSON.stringify(events[1])}\n\n`),
      ...parser.flush(),
    ]).toEqual(events);
  });
});
