const EMPTY_PEAKS = Object.freeze({ decodeTps: 0, prefillTps: 0 });

export function trackPeaks(peaks = EMPTY_PEAKS, stats) {
  return {
    decodeTps: Math.max(
      peaks.decodeTps,
      Number(stats?.throughput?.decode_tps) || 0,
    ),
    prefillTps: Math.max(
      peaks.prefillTps,
      Number(stats?.throughput?.prefill_tps) || 0,
    ),
  };
}

export function deriveModelState(inFlight, firstTokenSeen) {
  if (!inFlight) return "idle";
  return firstTokenSeen ? "generating" : "prefilling";
}

function hasText(value) {
  return typeof value === "string" && value.length > 0;
}

export function countTurnTokens(chunks) {
  return chunks.reduce(
    (counts, chunk) => ({
      reasoningTokens:
        counts.reasoningTokens + (hasText(chunk.reasoning_content) ? 1 : 0),
      answerTokens: counts.answerTokens + (hasText(chunk.content) ? 1 : 0),
    }),
    { reasoningTokens: 0, answerTokens: 0 },
  );
}

export function calculateTurnMetrics(startedAt, chunks) {
  const outputChunks = chunks.filter(
    (chunk) => hasText(chunk.reasoning_content) || hasText(chunk.content),
  );
  const { reasoningTokens, answerTokens } = countTurnTokens(chunks);
  const firstReasoning = chunks.find((chunk) =>
    hasText(chunk.reasoning_content),
  );
  const firstAnswer = chunks.find((chunk) => hasText(chunk.content));

  if (!outputChunks.length) {
    return {
      ttftMs: null,
      tokensPerSecond: 0,
      timeToFirstReasoningMs: null,
      timeToFirstAnswerMs: null,
      reasoningTokens,
      answerTokens,
    };
  }

  const first = outputChunks[0];
  const last = outputChunks[outputChunks.length - 1];
  const tokenCount = reasoningTokens + answerTokens;
  const generationMs = last.at - first.at;

  return {
    ttftMs: Math.max(0, first.at - startedAt),
    tokensPerSecond:
      tokenCount > 1 && generationMs > 0
        ? ((tokenCount - 1) * 1000) / generationMs
        : 0,
    timeToFirstReasoningMs: firstReasoning
      ? Math.max(0, firstReasoning.at - startedAt)
      : null,
    timeToFirstAnswerMs: firstAnswer
      ? Math.max(0, firstAnswer.at - startedAt)
      : null,
    reasoningTokens,
    answerTokens,
  };
}

export function formatBytes(bytes) {
  const value = Number(bytes);
  if (!Number.isFinite(value) || value < 0) return "0.0 GB";
  return `${(value / 1_000_000_000).toFixed(1)} GB`;
}

export function formatRate(rate) {
  const value = Number(rate);
  return `${(Number.isFinite(value) ? value : 0).toFixed(1)}`;
}

export function classifyLayerTier(layers, layerIndex) {
  const value = layers?.[String(layerIndex)];
  return typeof value === "number" && Number.isFinite(value) && value > 0
    ? "disk"
    : "resident";
}

export function diffExpertHits(previous, current) {
  if (!previous?.expert_hits || !current?.expert_hits) return [];

  const fired = [];
  for (const [layerKey, currentHits] of Object.entries(current.expert_hits)) {
    const previousHits = previous.expert_hits[layerKey];
    const layer = Number(layerKey);
    if (
      !Number.isInteger(layer) ||
      layer < 0 ||
      !Array.isArray(currentHits) ||
      !Array.isArray(previousHits)
    ) {
      continue;
    }

    currentHits.forEach((value, expert) => {
      const before = previousHits[expert];
      if (
        typeof value !== "number" ||
        !Number.isFinite(value) ||
        typeof before !== "number" ||
        !Number.isFinite(before)
      ) {
        return;
      }
      const delta = value - before;
      if (delta > 0) fired.push({ layer, expert, delta });
    });
  }
  return fired;
}

export function decayActivity(value, factor = 0.82, cutoff = 0.03) {
  const current = Number(value);
  if (!Number.isFinite(current) || current <= 0) return 0;
  const next = current * factor;
  return next < cutoff ? 0 : next;
}

function nonNegativeInteger(value) {
  return Number.isInteger(value) && value >= 0 ? value : null;
}

function positiveInteger(value) {
  return Number.isInteger(value) && value > 0 ? value : null;
}

export function calculateTierSummary(geometry) {
  const numExperts = positiveInteger(geometry?.num_experts);
  const numLayers = positiveInteger(geometry?.num_moe_layers);
  const requestedCacheSize = nonNegativeInteger(geometry?.moe_cache_size);
  const bytesPerExpert = positiveInteger(geometry?.unit_bytes?.moe_per_expert);
  const totalExperts =
    numExperts === null || numLayers === null ? null : numExperts * numLayers;
  const gpuExperts =
    totalExperts === null || requestedCacheSize === null
      ? null
      : Math.min(totalExperts, requestedCacheSize);
  const nvmeExperts =
    totalExperts === null || gpuExperts === null
      ? null
      : totalExperts - gpuExperts;

  return {
    totalExperts,
    gpuExperts,
    nvmeExperts,
    gpuBytes:
      gpuExperts === null || bytesPerExpert === null
        ? null
        : gpuExperts * bytesPerExpert,
    nvmeBytes:
      nvmeExperts === null || bytesPerExpert === null
        ? null
        : nvmeExperts * bytesPerExpert,
  };
}
