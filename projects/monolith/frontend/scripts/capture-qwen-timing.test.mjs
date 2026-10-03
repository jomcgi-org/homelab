import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readFile, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { captureTiming } from "./capture-qwen-timing.mjs";

for (const mixed of [false, true]) {
  test(
    mixed
      ? "rejects a recording when another request completed"
      : "counts server tokens rather than streamed events and saves only observed stats",
    async () => {
      const directory = await mkdtemp(join(tmpdir(), "qwen-capture-"));
      const output = join(directory, "recording.json");
      await writeFile(output, "original");
      const originalFetch = globalThis.fetch;
      let completed = false;
      globalThis.fetch = async (url, options) => {
        if (options?.method === "POST")
          return new Response(
            new ReadableStream({
              async start(controller) {
                const encoder = new TextEncoder();
                controller.enqueue(
                  encoder.encode(
                    'data: {"choices":[{"delta":{"content":"Hello"}}]}\n',
                  ),
                );
                await new Promise((resolve) => setTimeout(resolve, 20));
                controller.enqueue(
                  encoder.encode(
                    'data: {"choices":[{"delta":{"content":" world"},"finish_reason":"stop"}],"usage":{"prompt_tokens":8,"completion_tokens":5}}\ndata: [DONE]\n',
                  ),
                );
                completed = true;
                controller.close();
              },
            }),
          );
        return Response.json({
          instance_id: "one",
          requests: { active: 0, completed: completed ? (mixed ? 2 : 1) : 0 },
          model: { id: "served-model" },
          kv: { used_pages: 2, total_pages: 10 },
          throughput: { decode_tps: 20 },
          vram_bytes: 24e9,
        });
      };
      try {
        if (mixed) {
          await assert.rejects(
            captureTiming("http://local", "a".repeat(40), output),
            /mixed-request/,
          );
          assert.equal(await readFile(output, "utf8"), "original");
        } else {
          await captureTiming("http://local", "a".repeat(40), output);
          const recording = JSON.parse(await readFile(output, "utf8"));
          assert.equal(recording.turns[0].metrics.completionTokens, 5);
          assert.equal(recording.turns[0].events.length, 2);
          assert.equal(recording.telemetry.routing, false);
          assert.equal(recording.turns[0].statsSamples[0].vramBytes, 24e9);
          assert.ok(recording.turns[0].metrics.tokensPerSecond > 0);
          assert.equal(recording.instance_id, undefined);
        }
      } finally {
        globalThis.fetch = originalFetch;
        await rm(directory, { recursive: true });
      }
    },
  );
}
