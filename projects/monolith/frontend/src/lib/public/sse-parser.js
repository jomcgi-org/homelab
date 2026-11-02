/**
 * Parse a streamed SSE buffer into complete frames.
 *
 * Frames are blocks terminated by a blank line; only `data:` lines are read and
 * JSON-parsed. `push` returns the frames completed by the chunk just appended;
 * `flush` drains any trailing block that arrived without a final blank line.
 *
 * @returns {{ push: (chunk: string) => object[], flush: () => object[] }}
 */
export function createSseParser() {
  let buffer = "";

  function parseBlock(block) {
    const dataLines = [];
    for (const raw of block.split("\n")) {
      const line = raw.replace(/\r$/, "");
      if (line.startsWith("data:")) {
        // Strip the `data:` prefix and a single optional leading space.
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
      // Normalize CRLF to LF so the blank-line split works whether the
      // upstream framed with \n\n (our backend) or \r\n\r\n (a proxy that
      // rewrites line endings). JSON-encoded token text never carries raw
      // newlines, so this only ever touches SSE framing.
      buffer += chunk.replace(/\r\n/g, "\n");
      const frames = [];
      let idx;
      while ((idx = buffer.indexOf("\n\n")) !== -1) {
        const block = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
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
