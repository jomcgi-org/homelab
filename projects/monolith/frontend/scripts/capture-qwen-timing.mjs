import { writeFile } from "node:fs/promises";
import { performance } from "node:perf_hooks";

// Observe the normal serving configuration without enabling diagnostic routing counters.
export async function captureTiming(base, build, output) {
  const getStats = async () => {
    const response = await fetch(`${base}/v1/stats`, {
      signal: AbortSignal.timeout(10000),
    });
    if (!response.ok) throw new Error(`Stats: ${response.status}`);
    return response.json();
  };
  const before = await getStats();
  if (before.requests.active !== 0)
    throw new Error("Service is busy; capture not started.");
  const prompt =
    "Explain how a 125B mixture-of-experts model generates tokens on a 24GB RTX 4090 and 64GB RAM in three short sentences, under 80 words. GPU-resident experts compute locally. For pinned weights, a hybrid executor limits PCIe fetches and computes the other experts on the CPU. Cold experts use the CPU and page cache, reading from NVMe on a cache miss. Explain how the outputs combine.";
  const turn = {
    title: "How this inference works",
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
  let mixed = false;
  async function sample() {
    try {
      const stats = await getStats();
      if (stats.instance_id !== before.instance_id || stats.requests.active > 1)
        mixed = true;
      turn.statsSamples.push({
        at: elapsed(),
        kvUsedPages: stats.kv?.used_pages ?? null,
        kvTotalPages: stats.kv?.total_pages ?? null,
        activeRequests: stats.requests.active,
        decodeTps: stats.throughput?.decode_tps ?? null,
        vramBytes: stats.vram_bytes ?? null,
      });
    } catch {
      turn.statsSamples.push({ at: elapsed(), unavailable: true });
    }
  }
  await sample();
  const poll = (async () => {
    while (!stopped) {
      await new Promise((resolve) => setTimeout(resolve, 200));
      if (!stopped) await sample();
    }
  })();
  try {
    const response = await fetch(`${base}/v1/chat/completions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        model: before.model.id,
        messages: [{ role: "user", content: prompt }],
        stream: true,
        stream_options: { include_usage: true },
        max_tokens: 256,
        chat_template_kwargs: { enable_thinking: false },
      }),
      signal: AbortSignal.timeout(120000),
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
        if (choice?.delta?.content || choice?.delta?.reasoning_content)
          turn.events.push({
            at: elapsed(),
            content: choice.delta.content ?? "",
            reasoning: choice.delta.reasoning_content ?? "",
          });
      }
    }
    turn.durationMs = elapsed();
  } finally {
    stopped = true;
    await poll;
  }
  await sample();
  const after = await getStats();
  if (
    mixed ||
    after.instance_id !== before.instance_id ||
    after.requests.active !== 0 ||
    after.requests.completed !== before.requests.completed + 1 ||
    !turn.events.length ||
    turn.finishReason !== "stop" ||
    !Number.isInteger(turn.usage?.completion_tokens)
  )
    throw new Error(
      "Incomplete or mixed-request capture; recording not saved.",
    );
  const first = turn.events[0].at;
  const last = turn.events.at(-1).at;
  turn.durationMs = Math.max(
    turn.durationMs,
    ...turn.statsSamples.map((sample) => sample.at),
  );
  turn.metrics = {
    ttftMs: first,
    generationMs: last - first,
    completionTokens: turn.usage.completion_tokens,
    tokensPerSecond:
      last > first
        ? ((turn.usage.completion_tokens - 1) * 1000) / (last - first)
        : null,
  };
  const recording = {
    version: 3,
    recordedAt: new Date().toISOString(),
    build,
    model: "Qwen3.8-Flash-Next, 125B, NVFP4",
    hardware: "RTX 4090 24 GB, Ryzen 7800X3D, 64 GB RAM, NVMe",
    thinking: false,
    sampleIntervalMs: 200,
    conditions:
      "Existing warm shared service. Default sampling, thinking disabled, prefix reuse available. Client timings include transport. Normal hybrid expert execution, staged prefill and FP8 dense weights. Expert-routing profiling disabled. One request, not an isolated benchmark.",
    telemetry: { routing: false, stats: true },
    turns: [turn],
  };
  await writeFile(output, JSON.stringify(recording) + "\n");
  console.log(JSON.stringify(turn.metrics));
}
