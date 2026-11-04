import { readFileSync } from "node:fs";
import { describe, expect, it, vi } from "vitest";
import { consumeSseStream } from "../sse-stream.js";

const privateSource = readFileSync(
  new URL("../../routes/private/chat/+page.svelte", import.meta.url),
  "utf8",
);
const clusterSource = readFileSync(
  new URL("./components/ClusterChatPanel.svelte", import.meta.url),
  "utf8",
);

// Execute the actual caller function with synthetic component state. Rendering
// and Svelte lifecycle are outside this transport characterization.
async function runCaller(source, name, wire, overrides = {}) {
  const start = source.indexOf(`  async function ${name}()`);
  const end = source.indexOf("\n  function onKeydown", start);
  const body = new ReadableStream({
    start(controller) {
      for (const chunk of wire) {
        controller.enqueue(new TextEncoder().encode(chunk));
      }
      controller.close();
    },
  });
  const fetch = vi.fn(async () => ({ ok: true, body, ...overrides.response }));
  const graph = overrides.graph ?? vi.fn();
  const scroll = vi.fn();
  const execute = new Function(
    "fetch",
    "enqueueGraphEvent",
    "scrollToBottom",
    "consumeSseStream",
    `return (async () => {
      let inputText = "question", input = "question";
      let isStreaming = false, streaming = false;
      const HISTORY_CAP = 12;
      const messages = [{ role: "assistant", content: "prior" }];
      const formatArgs = (args) => Object.values(args).join(" ");
      ${source.slice(start, end)}
      await ${name}();
      return { messages, isStreaming, streaming };
    })();`,
  );
  expect(start).toBeGreaterThan(-1);
  expect(end).toBeGreaterThan(start);
  const result = await execute(
    fetch,
    graph,
    overrides.scroll ?? scroll,
    consumeSseStream,
  );
  return { ...result, fetch, graph, body };
}

describe("private caller framing", () => {
  for (const [label, source, name] of [
    ["private", privateSource, "sendMessage"],
    ["cluster", clusterSource, "send"],
  ]) {
    for (const [framing, wire] of [
      ["no-space", ['data:{"type":"text_chunk","data":{"text":"hello"}}\n\n']],
      ["CRLF", ['data: {"type":"text_chunk","data":{"text":"hello"}}\r\n\r\n']],
      ["EOF", ['data: {"type":"text_chunk","data":{"text":"hello"}}']],
    ]) {
      it(`${label} accepts ${framing}`, async () => {
        const result = await runCaller(source, name, wire);
        expect(result.messages.at(-1).content).toBe("hello");
        expect(result.body.locked).toBe(false);
      });
    }
    it(`${label} preserves request boundaries and per-event dispatch failures`, async () => {
      const events = [
        { type: "text_chunk", data: { text: "first" } },
        { type: "text_chunk" }, // Missing data throws inside caller dispatch.
        { type: "error", data: { message: "backend error" } },
        { type: "node_discovered", data: { id: "graph" } },
        { type: "tool_call", data: { tool: "inspect", args: { pod: "test" } } },
        { type: "done" },
        { type: "text_chunk", data: { text: "last" } },
      ];
      const wire = events
        .map((event) => `data: ${JSON.stringify(event)}\n\n`)
        .join("");
      const result = await runCaller(source, name, Array.from(wire));
      expect(result.fetch).toHaveBeenCalledExactlyOnceWith(
        label === "private" ? "/private/chat" : "/dashboard-chat",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            message: "question",
            history: [{ role: "assistant", content: "prior" }],
          }),
        },
      );
      const current = result.messages.at(-1);
      if (label === "private") {
        expect(current.content).toBe("first\n[Error: backend error]last");
        expect(result.graph).toHaveBeenCalledWith(events[3]);
        expect(result.graph).toHaveBeenCalledTimes(2);
      } else {
        expect(current.content).toBe("firstlast");
        expect(current.error).toBe("backend error");
        expect(current.tools).toEqual(["> inspect test"]);
        expect(result.graph).not.toHaveBeenCalled();
      }
      expect(result.body.locked).toBe(false);
      expect(result.isStreaming).toBe(false);
      expect(result.streaming).toBe(false);
    });

    it(`${label} keeps HTTP and missing-body errors in the caller`, async () => {
      for (const response of [{ ok: false, status: 403 }, { body: null }]) {
        const result = await runCaller(source, name, [], { response });
        if (label === "private") {
          expect(result.messages.at(-1).content).toContain(
            "[Connection error:",
          );
          if (response.status)
            expect(result.messages.at(-1).content).toContain("HTTP 403");
        } else {
          expect(result.messages.at(-1).error).not.toBe("");
          if (response.status)
            expect(result.messages.at(-1).error).toBe("HTTP 403");
        }
      }
    });
  }
  it("private graph dispatch failures stay local to the event", async () => {
    const result = await runCaller(
      privateSource,
      "sendMessage",
      [
        'data: {"type":"node_discovered"}\n\ndata: {"type":"text_chunk","data":{"text":"after"}}\n\n',
      ],
      {
        graph: vi.fn(() => {
          throw new Error("UI dispatch");
        }),
      },
    );
    expect(result.messages.at(-1).content).toBe("after");
    expect(result.body.locked).toBe(false);
  });
});
