// Parsed public graph only, bounded by its HTTP lease (including response age).
let cache = null;
export function getCachedGraph(now = Date.now()) {
  if (!cache || now >= cache.expiresAt) {
    cache = null;
    return null;
  }
  return cache.graph;
}
export function graphExpiresAt(headers, now = Date.now()) {
  const policy = headers?.get("cache-control") || "";
  const ttl = Number(policy.match(/(?:^|,)\s*max-age=(\d+)/)?.[1] || 0);
  const date = Date.parse(headers?.get("date"));
  const age = Math.max(
    Number(headers?.get("age") || 0),
    Number.isFinite(date) ? Math.max(0, (now - date) / 1000) : 0,
  );
  const remaining = /(?:no-store|no-cache|private)/.test(policy)
    ? 0
    : ttl - age;
  return remaining > 0 && Number.isFinite(remaining)
    ? now + remaining * 1000
    : now;
}
export function setCachedGraph(graph, headers, now = Date.now()) {
  const expiresAt = graphExpiresAt(headers, now);
  cache =
    expiresAt > now
      ? {
          graph: { ...graph, expiresAt },
          expiresAt,
        }
      : null;
  return cache?.expiresAt ?? now;
}
