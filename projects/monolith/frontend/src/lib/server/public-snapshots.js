import { version } from "$app/environment";
import { boundedCacheHeaders, versionedEtag } from "../cache-headers.js";

// Fixed anonymous projections only. No caller URL, credentials, query or
// private route can enter this cache. Existing factory snapshots are published
// by private jobs; the graph is materialized from the public_reader projection.
export const PUBLIC_SNAPSHOTS = Object.freeze({
  activity: "/api/agents/public/activity",
  merges: "/api/agents/public/merges",
  goals: "/api/agents/public/factory/goals",
  board: "/api/agents/public/factory/activity",
  facts: "/api/knowledge/public/facts/daily",
  graph: "/api/knowledge/public/graph",
});
const POLICY = "public, max-age=60, s-maxage=60, must-revalidate";
const MAX_BYTES = 16 * 1024 * 1024;

export function createPublicSnapshotCache({ fetcher, base, now = Date.now }) {
  const entries = new Map();
  const pending = new Map();
  const retryAfter = new Map();
  const refreshAfter = new Map();
  const key = (name) => `${version}:${base}:${name}`;

  function headersFor(entry) {
    const ttl = Math.floor((entry.expiresAt - now()) / 1000);
    const headers =
      ttl > 0
        ? {
            "cache-control": `public, max-age=${ttl}, s-maxage=${ttl}, must-revalidate`,
            "cloudflare-cdn-cache-control": `public, max-age=${ttl}, must-revalidate`,
            date: new Date(now()).toUTCString(),
          }
        : {
            "cache-control": "no-store",
            "cloudflare-cdn-cache-control": "no-store",
          };
    const etag = versionedEtag(entry.headers.get("etag"));
    if (etag) headers.etag = etag;
    const modified = entry.headers.get("last-modified");
    if (modified) headers["last-modified"] = modified;
    return headers;
  }

  async function refresh(name) {
    const id = key(name);
    if (pending.has(id)) return pending.get(id);
    const work = (async () => {
      try {
        const response = await fetcher(`${base}${PUBLIC_SNAPSHOTS[name]}`, {
          credentials: "omit",
          redirect: "error",
          headers: { accept: "application/json" },
          signal: AbortSignal.timeout(10_000),
        });
        if (
          !response.ok ||
          response.headers.has("set-cookie") ||
          /(?:^|,)\s*private(?:,|$)/i.test(
            response.headers.get("cache-control") || "",
          )
        ) {
          throw new Error("public snapshot unavailable");
        }
        const headers = new Headers(response.headers);
        // Anchor missing Date at receipt, before buffering/parsing the body.
        if (!headers.has("date"))
          headers.set("date", new Date(now()).toUTCString());
        const policy = boundedCacheHeaders(POLICY, headers, now())[
          "cache-control"
        ];
        const ttl = Number(policy.match(/s-maxage=(\d+)/)?.[1] || 0);
        if (
          !ttl &&
          /s-maxage=[1-9]/i.test(headers.get("cache-control") || "") &&
          !/(?:no-store|no-cache)/i.test(headers.get("cache-control") || "")
        ) {
          throw new Error("origin snapshot expired");
        }
        const expiresAt = now() + ttl * 1000;
        const body = await response.text();
        if (Buffer.byteLength(body) > MAX_BYTES)
          throw new Error("snapshot too large");
        JSON.parse(body); // Never retain malformed/error HTML as successful JSON.
        const entry = { body, headers, expiresAt };
        if (ttl > 0 && expiresAt <= now())
          throw new Error("snapshot expired during read");
        if (headersFor(entry)["cache-control"] === "no-store")
          entries.delete(id);
        else entries.set(id, entry);
        retryAfter.delete(id);
        refreshAfter.set(id, now() + 10_000);
        return entry;
      } catch (error) {
        // Bound failed prewarm/retry traffic. A valid prior snapshot can still
        // be used within its lease; an expired one is never a fallback.
        retryAfter.set(id, now() + 5000);
        throw error;
      } finally {
        pending.delete(id);
      }
    })();
    pending.set(id, work);
    return work;
  }

  async function read(name) {
    if (!Object.hasOwn(PUBLIC_SNAPSHOTS, name))
      throw new Error("unknown public snapshot");
    const id = key(name);
    const entry = entries.get(id);
    const headers = entry && headersFor(entry);
    if (headers && headers["cache-control"] !== "no-store") {
      const ttl = Number(
        headers["cache-control"].match(/s-maxage=(\d+)/)?.[1] || 0,
      );
      // Refresh before expiry while serving only still-fresh bytes. The same
      // promise serves concurrent cold requests and the background refresher.
      if (
        ttl <= 20 &&
        Math.max(retryAfter.get(id) || 0, refreshAfter.get(id) || 0) <= now()
      ) {
        void refresh(name).catch(() => {});
      }
      return { body: entry.body, headers };
    }
    entries.delete(id);
    if ((retryAfter.get(id) || 0) > now())
      throw new Error("snapshot refresh backoff");
    const loaded = await refresh(name);
    return { body: loaded.body, headers: headersFor(loaded) };
  }

  return {
    read,
    prewarm: () => Promise.allSettled(Object.keys(PUBLIC_SNAPSHOTS).map(read)),
  };
}

let cache;
function current() {
  // API_BASE is deployment-owned, never a caller-supplied destination. Only the
  // public chart enables startup prewarm; private deployments do not run it.
  if (!process.env.API_BASE) throw new Error("API_BASE required");
  cache ??= createPublicSnapshotCache({
    base: process.env.API_BASE,
    fetcher: (...args) => globalThis.fetch(...args),
  });
  return cache;
}
export const readPublicSnapshot = (name) => current().read(name);
export function startPublicSnapshotPrewarm() {
  if (process.env.PUBLIC_SNAPSHOT_PREWARM !== "true") return;
  void current().prewarm();
  const timer = setInterval(() => void current().prewarm(), 10_000);
  timer.unref();
}
