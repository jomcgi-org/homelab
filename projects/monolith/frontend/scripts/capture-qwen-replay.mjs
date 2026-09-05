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
    "Walk through one token on this 4090 in three short sentences, under 80 words. Hot experts stay on the GPU. Warm experts transfer from pinned RAM over PCIe. Cold experts run on the CPU using weights from the page cache or NVMe. Explain how their outputs combine to generate the next token.",
  ],
];
const recording = {
  version: 2,
  recordedAt: new Date().toISOString(),
  build,
  model: "Qwen3.8-Flash-Next, 125B, NVFP4",
  hardware: "RTX 4090 24 GB, Ryzen 7800X3D, 64 GB RAM, NVMe",
  thinking: false,
  sampleIntervalMs,
  conditions:
    "Shared serving setup, default sampling, one request observed during capture. Client timings include transport. Prefill rates use completed CUDA event intervals, including transfers and host dispatch gaps, not queueing or transport. This is a walkthrough, not an isolated benchmark.",
  turns: [],
};
const messages = [];
for (const [title, prompt] of prompts) {
  messages.push({ role: "user", content: prompt });
  let previous = await get("/v1/moe-layer-profile");
  const cache = await get("/v1/cache/status");
  const before = await get("/v1/stats");
  if (before.requests.active !== 0 || !previous.prefill?.chunks) {
    throw new Error(
      "Capture requires an idle service with prefill chunk telemetry.",
    );
  }
  const baselineSequence = previous.prefill.chunks.at(-1)?.sequence ?? 0;
  const seenChunks = new Set();
  let invalidCapture = false;
  const turn = {
    title,
    prompt,
    events: [],
    samples: [],
    statsSamples: [],
    prefillChunks: [],
    usage: null,
    finishReason: null,
  };
  const start = performance.now();
  const elapsed = () => Math.round(performance.now() - start);
  let stopped = false;
  const sample = async () => {
    try {
      const profile = await get("/v1/moe-layer-profile");
      for (const chunk of profile.prefill?.chunks ?? []) {
        if (
          chunk.sequence <= baselineSequence ||
          seenChunks.has(chunk.sequence)
        )
          continue;
        seenChunks.add(chunk.sequence);
        turn.prefillChunks.push({
          // Availability is the receipt time. Do not pretend host observation
          // timestamps identify exact CUDA completion times on the client.
          at: elapsed(),
          sequence: chunk.sequence,
          elapsedMs: chunk.elapsed_ms,
          tokens: chunk.tokens,
          tokensPerSecond: chunk.tokens_per_second,
          requests: chunk.requests,
        });
      }
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
      if (stats.instance_id !== before.instance_id || stats.requests.active > 1)
        invalidCapture = true;
      turn.statsSamples.push({
        at: elapsed(),
        kvUsedPages: stats.kv?.used_pages ?? null,
        kvTotalPages: stats.kv?.total_pages ?? null,
        activeRequests: stats.requests?.active ?? null,
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
  const after = await get("/v1/stats");
  const uids = new Set(
    turn.prefillChunks.flatMap((chunk) => chunk.requests.map((r) => r.uid)),
  );
  const processedTokens = turn.prefillChunks.reduce(
    (sum, chunk) => sum + chunk.tokens,
    0,
  );
  if (
    invalidCapture ||
    after.instance_id !== before.instance_id ||
    after.requests.active !== 0 ||
    after.requests.completed !== before.requests.completed + 1 ||
    !Number.isInteger(turn.usage?.prompt_tokens) ||
    uids.size !== 1 ||
    !turn.prefillChunks.length ||
    processedTokens > turn.usage?.prompt_tokens ||
    turn.prefillChunks.at(-1)?.requests[0]?.completed_tokens !==
      turn.usage?.prompt_tokens ||
    turn.prefillChunks.some(
      (chunk, index) =>
        chunk.requests.length !== 1 ||
        !Number.isFinite(chunk.elapsedMs) ||
        !(chunk.elapsedMs > 0) ||
        chunk.sequence !== baselineSequence + index + 1 ||
        (index > 0 &&
          chunk.requests[0].completed_tokens !==
            turn.prefillChunks[index - 1].requests[0].completed_tokens +
              chunk.tokens),
    )
  ) {
    throw new Error(
      "Incomplete or mixed-request prefill telemetry; recording not saved.",
    );
  }
  // Internal request ids are only needed for attribution checks, not publication.
  for (const chunk of turn.prefillChunks) {
    chunk.completedTokens = chunk.requests[0].completed_tokens;
    delete chunk.requests;
  }
  turn.metrics = {
    ttftMs: first,
    generationMs: last - first,
    // Usage is reported by the server. Never count streamed chunks as tokens.
    completionTokens: turn.usage?.completion_tokens ?? null,
    prefillTokens: processedTokens,
    cachedPromptTokens: turn.usage.prompt_tokens - processedTokens,
    prefillMs: turn.prefillChunks.reduce(
      (sum, chunk) => sum + chunk.elapsedMs,
      0,
    ),
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
