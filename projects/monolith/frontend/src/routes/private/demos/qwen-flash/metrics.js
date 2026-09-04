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
