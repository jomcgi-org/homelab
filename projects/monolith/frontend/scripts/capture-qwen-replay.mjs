// Capture through a local tunnel to the demo's upstream. No service credentials
// or raw server profiles are written into the public recording.
// QWEN_REPLAY_API=http://127.0.0.1:18090 QWEN_REPLAY_BUILD=<sha> node scripts/capture-qwen-replay.mjs
import { writeFile } from "node:fs/promises";
import { performance } from "node:perf_hooks";
import {
  attributeExpertActivity,
  calculateTierSummary,
} from "../src/routes/private/demos/qwen-flash/metrics.js";

const base = process.env.QWEN_REPLAY_API;
const build = process.env.QWEN_REPLAY_BUILD;
const sampleIntervalMs = 200;
if (!base || !/^[a-f0-9]{40}$/.test(build ?? "")) {
  throw new Error(
    "Set QWEN_REPLAY_API and the verified QWEN_REPLAY_BUILD commit.",
  );
}
const get = async (path) => {
  const response = await fetch(`${base}${path}`, {
    signal: AbortSignal.timeout(10000),
  });
  if (!response.ok) throw new Error(`${path}: ${response.status}`);
  return response.json();
};
const prompts = [
  [
    "How this inference works",
    "Explain this inference setup in three short sentences, under 80 words: hot experts stay on the GPU, warm experts transfer from pinned RAM over PCIe, and cold experts run on the CPU using weights from the page cache or NVMe. Explain how their outputs combine to generate the next token.",
  ],
];
const recording = {
  version: 1,
  recordedAt: new Date().toISOString(),
  build,
  model: "Qwen3.8-Flash-Next, 125B, NVFP4",
  hardware: "RTX 4090 24 GB, Ryzen 7800X3D, 64 GB RAM, NVMe",
  thinking: false,
  sampleIntervalMs,
  conditions:
    "Existing warm service, default sampling, shared with normal traffic. Client timings include transport. This is a walkthrough, not an isolated benchmark.",
  turns: [],
};
const messages = [];
for (const [title, prompt] of prompts) {
  messages.push({ role: "user", content: prompt });
  let previous = await get("/v1/moe-layer-profile");
  const cache = await get("/v1/cache/status");
  const turn = {
    title,
    prompt,
    events: [],
    samples: [],
    statsSamples: [],
    usage: null,
    finishReason: null,
  };
  const start = performance.now();
  const elapsed = () => Math.round(performance.now() - start);
  let stopped = false;
  const sample = async () => {
    try {
      const profile = await get("/v1/moe-layer-profile");
      turn.samples.push({
        at: elapsed(),
        activity: attributeExpertActivity(previous, profile, cache.geometry),
        tiers: calculateTierSummary(profile, cache.geometry),
      });
      previous = profile;
    } catch (error) {
      turn.samples.push({ at: elapsed(), unavailable: true });
      console.error(`Telemetry unavailable: ${error.message}`);
    }
  };
  const sampleStats = async () => {
    try {
      const stats = await get("/v1/stats");
      turn.statsSamples.push({
        at: elapsed(),
        kvUsedPages: stats.kv?.used_pages ?? null,
        kvTotalPages: stats.kv?.total_pages ?? null,
        activeRequests: stats.requests?.active ?? null,
        prefillTps: stats.throughput?.prefill_tps ?? null,
        decodeTps: stats.throughput?.decode_tps ?? null,
      });
    } catch {
      turn.statsSamples.push({ at: elapsed(), unavailable: true });
    }
  };
  const pollEvery = async (task) => {
    let nextSampleAt = performance.now() + sampleIntervalMs;
    while (!stopped) {
      await new Promise((resolve) =>
        setTimeout(resolve, Math.max(0, nextSampleAt - performance.now())),
      );
      if (stopped) break;
      nextSampleAt = performance.now() + sampleIntervalMs;
      await task();
    }
  };
  const profilePoll = pollEvery(sample);
  const statsPoll = pollEvery(sampleStats);
  try {
    const response = await fetch(`${base}/v1/chat/completions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        model: "qwen3.6-27b",
        messages,
        stream: true,
        stream_options: { include_usage: true },
        max_tokens: 512,
        chat_template_kwargs: { enable_thinking: false },
      }),
      signal: AbortSignal.timeout(300000),
    });
    if (!response.ok) throw new Error(`Chat: ${response.status}`);
    let pending = "";
    for await (const chunk of response.body.pipeThrough(
      new TextDecoderStream(),
    )) {
      pending += chunk;
      const lines = pending.split("\n");
      pending = lines.pop();
      for (const line of lines) {
        if (!line.startsWith("data: ") || line.trim() === "data: [DONE]")
          continue;
        const data = JSON.parse(line.slice(6));
        if (data.error) throw new Error(JSON.stringify(data.error));
        if (data.usage)
          turn.usage = {
            prompt_tokens: data.usage.prompt_tokens,
            completion_tokens: data.usage.completion_tokens,
          };
        const choice = data.choices?.[0];
        if (choice?.finish_reason) turn.finishReason = choice.finish_reason;
        const delta = choice?.delta;
        if (delta?.content || delta?.reasoning_content)
          turn.events.push({
            at: elapsed(),
            content: delta.content ?? "",
            reasoning: delta.reasoning_content ?? "",
          });
      }
    }
    turn.durationMs = elapsed();
  } finally {
    stopped = true;
    await Promise.all([profilePoll, statsPoll]);
  }
  await Promise.all([sample(), sampleStats()]);
  turn.durationMs = Math.max(
    turn.durationMs,
    ...turn.samples.map((sample) => sample.at),
    ...turn.statsSamples.map((sample) => sample.at),
  );
  if (!turn.events.length || !turn.finishReason)
    throw new Error("Incomplete response; recording not saved.");
  const first = turn.events[0].at;
  const last = turn.events.at(-1).at;
  turn.metrics = {
    ttftMs: first,
    generationMs: last - first,
    // Usage is reported by the server. Never count streamed chunks as tokens.
    completionTokens: turn.usage?.completion_tokens ?? null,
    tokensPerSecond:
      Number.isInteger(turn.usage?.completion_tokens) && last > first
        ? ((turn.usage.completion_tokens - 1) * 1000) / (last - first)
        : null,
  };
  messages.push({
    role: "assistant",
    content: turn.events.map((e) => e.content).join(""),
  });
  recording.turns.push(turn);
  console.log(
    `${title}: ${turn.events.length} chunks, ${turn.metrics.ttftMs} ms first token, ${turn.metrics.completionTokens ?? "unknown"} tokens`,
  );
}
await writeFile(
  new URL("../src/lib/public/posts/qwen-replay.json", import.meta.url),
  JSON.stringify(recording) + "\n",
);
