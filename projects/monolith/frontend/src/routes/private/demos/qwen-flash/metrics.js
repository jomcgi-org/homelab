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

export function calculateTurnMetrics(startedAt, chunks) {
  if (!chunks.length) return { ttftMs: null, tokensPerSecond: 0 };

  const first = chunks[0];
  const last = chunks[chunks.length - 1];
  const tokenCount = chunks.reduce(
    (total, chunk) => total + (Number(chunk.tokens) || 0),
    0,
  );
  const generationMs = last.at - first.at;

  return {
    ttftMs: Math.max(0, first.at - startedAt),
    tokensPerSecond:
      tokenCount > 1 && generationMs > 0
        ? ((tokenCount - 1) * 1000) / generationMs
        : 0,
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
