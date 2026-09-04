const EMPTY_PEAKS = Object.freeze({ decodeTps: 0, ttftMs: null });
const EMPTY_TOTALS = Object.freeze({ turns: 0, tokens: 0, generationMs: 0 });

function hasText(value) {
  return typeof value === "string" && value.length > 0;
}

function nonNegativeInteger(value) {
  return Number.isInteger(value) && value >= 0 ? value : null;
}

function positiveInteger(value) {
  return Number.isInteger(value) && value > 0 ? value : null;
}

function finiteNonNegative(value) {
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? number : 0;
}

function indexedValues(value) {
  if (Array.isArray(value)) return value;
  if (!value || typeof value !== "object") return null;

  const indexes = Object.keys(value)
    .map(Number)
    .filter((index) => Number.isInteger(index) && index >= 0);
  if (!indexes.length) return [];

  const result = new Array(Math.max(...indexes) + 1);
  for (const index of indexes) result[index] = value[String(index)];
  return result;
}

function profileLayers(profile) {
  return indexedValues(profile?.layers);
}

function profileHits(profile) {
  return indexedValues(profile?.expert_hits);
}

function inferredExpertCount(profile) {
  const rows = profileHits(profile);
  if (!rows) return null;
  const widths = rows
    .filter(Array.isArray)
    .map((row) => row.length)
    .filter((width) => width > 0);
  if (!widths.length || widths.some((width) => width !== widths[0]))
    return null;
  return widths[0];
}

export function trackSessionPeaks(peaks = EMPTY_PEAKS, turnMetrics) {
  const decodeTps = finiteNonNegative(turnMetrics?.tokensPerSecond);
  const ttft = finiteNonNegative(turnMetrics?.ttftMs);
  const nextTtft =
    turnMetrics?.ttftMs === null || turnMetrics?.ttftMs === undefined
      ? peaks.ttftMs
      : peaks.ttftMs === null
        ? ttft
        : Math.min(peaks.ttftMs, ttft);

  return {
    decodeTps: Math.max(finiteNonNegative(peaks.decodeTps), decodeTps),
    ttftMs: nextTtft,
  };
}

export function addSessionTurn(
  totals = EMPTY_TOTALS,
  turnMetrics,
  generationMs,
) {
  return {
    turns: nonNegativeInteger(totals.turns) === null ? 1 : totals.turns + 1,
    tokens:
      finiteNonNegative(totals.tokens) +
      finiteNonNegative(turnMetrics?.reasoningTokens) +
      finiteNonNegative(turnMetrics?.answerTokens),
    generationMs:
      finiteNonNegative(totals.generationMs) + finiteNonNegative(generationMs),
  };
}

export function deriveModelState(inFlight, firstTokenSeen) {
  if (!inFlight) return "idle";
  return firstTokenSeen ? "generating" : "prefilling";
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
  const values = indexedValues(layers);
  const value = values?.[layerIndex];
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
    return "unknown";
  }
  return value > 0 ? "disk" : "resident";
}

export function diffExpertHits(previous, current) {
  const previousRows = profileHits(previous);
  const currentRows = profileHits(current);
  if (!previousRows || !currentRows) return [];

  const fired = [];
  currentRows.forEach((currentHits, layer) => {
    const previousHits = previousRows[layer];
    if (!Array.isArray(currentHits) || !Array.isArray(previousHits)) return;

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
  });
  return fired;
}

export function calculateTierSummary(profile, geometry) {
  const layers = profileLayers(profile);
  const configuredLayers = positiveInteger(geometry?.num_moe_layers);
  const numLayers = configuredLayers ?? (layers?.length || null);
  const numExperts =
    positiveInteger(geometry?.num_experts) ?? inferredExpertCount(profile);
  const requestedCacheSize = nonNegativeInteger(geometry?.moe_cache_size);
  const bytesPerExpert = positiveInteger(geometry?.unit_bytes?.moe_per_expert);
  const totalExperts =
    numExperts === null || numLayers === null ? null : numExperts * numLayers;

  let residentLayers = null;
  let diskLayers = null;
  if (layers && numLayers !== null && layers.length >= numLayers) {
    residentLayers = 0;
    diskLayers = 0;
    for (let layer = 0; layer < numLayers; layer += 1) {
      const tier = classifyLayerTier(layers, layer);
      if (tier === "resident") residentLayers += 1;
      else if (tier === "disk") diskLayers += 1;
      else {
        residentLayers = null;
        diskLayers = null;
        break;
      }
    }
  }

  const warmExperts =
    residentLayers === null || numExperts === null
      ? null
      : residentLayers * numExperts;
  const diskExperts =
    diskLayers === null || numExperts === null ? null : diskLayers * numExperts;
  const hotExperts =
    diskExperts === null
      ? null
      : diskExperts === 0
        ? 0
        : requestedCacheSize === null
          ? null
          : Math.min(diskExperts, requestedCacheSize);
  const coldExperts =
    diskExperts === null || hotExperts === null
      ? null
      : diskExperts - hotExperts;
  const bytes = (experts) =>
    experts === null || bytesPerExpert === null
      ? null
      : experts * bytesPerExpert;

  return {
    totalExperts,
    residentLayers,
    diskLayers,
    hotExperts,
    warmExperts,
    coldExperts,
    hotBytes: bytes(hotExperts),
    warmBytes: bytes(warmExperts),
    coldBytes: bytes(coldExperts),
  };
}

function likelyHotExperts(profile, geometry) {
  const summary = calculateTierSummary(profile, geometry);
  const layers = profileLayers(profile);
  const hits = profileHits(profile);
  const numExperts =
    positiveInteger(geometry?.num_experts) ?? inferredExpertCount(profile);
  if (
    !layers ||
    !hits ||
    numExperts === null ||
    summary.hotExperts === null ||
    summary.diskLayers === null
  ) {
    return null;
  }

  const diskLayerIndexes = layers
    .map((_, layer) => layer)
    .filter((layer) => classifyLayerTier(layers, layer) === "disk");
  const hot = new Set();
  if (!diskLayerIndexes.length || summary.hotExperts === 0) return hot;

  const perLayer = Math.floor(summary.hotExperts / diskLayerIndexes.length);
  let remainder = summary.hotExperts % diskLayerIndexes.length;
  for (const layer of diskLayerIndexes) {
    const slotCount = Math.min(numExperts, perLayer + (remainder > 0 ? 1 : 0));
    if (remainder > 0) remainder -= 1;
    const row = Array.isArray(hits[layer]) ? hits[layer] : [];
    const ranked = Array.from({ length: numExperts }, (_, expert) => ({
      expert,
      hits:
        typeof row[expert] === "number" && Number.isFinite(row[expert])
          ? row[expert]
          : Number.NEGATIVE_INFINITY,
    })).sort(
      (left, right) => right.hits - left.hits || left.expert - right.expert,
    );
    for (let index = 0; index < slotCount; index += 1) {
      hot.add(`${layer}:${ranked[index].expert}`);
    }
  }
  return hot;
}

export function attributeExpertActivity(previous, current, geometry) {
  const fired = diffExpertHits(previous, current);
  const layers = profileLayers(current);
  const hotExperts = likelyHotExperts(current, geometry);
  const activity = { hotHits: 0, warmHits: 0, coldHits: 0, unknownHits: 0 };

  for (const { layer, expert, delta } of fired) {
    const tier = classifyLayerTier(layers, layer);
    if (tier === "resident") activity.warmHits += delta;
    else if (tier === "disk" && hotExperts?.has(`${layer}:${expert}`)) {
      activity.hotHits += delta;
    } else if (tier === "disk" && hotExperts) activity.coldHits += delta;
    else activity.unknownHits += delta;
  }

  return {
    ...activity,
    totalHits:
      activity.hotHits +
      activity.warmHits +
      activity.coldHits +
      activity.unknownHits,
  };
}
