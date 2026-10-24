import { startPublicSnapshotPrewarm } from "./lib/server/public-snapshots.js";

export function init() {
  startPublicSnapshotPrewarm();
}

// SvelteKit server hook: negotiated response compression.
//
// adapter-node does not compress dynamic responses, and Cloudflare in front of
// us was serving them as identity (the public knowledge-graph JSON alone is
// ~3.4 MB; ships/stars/trips payloads and every SSR page went out raw too). The
// browser never reaches the FastAPI backend directly: every API response is
// fetched server-side inside this Node process and re-emitted through a
// +server.js proxy or inlined by a +page.server.js load(). That makes this
// server the single chokepoint every browser-facing byte passes through, so we
// compress here, once, and it covers all of them.
//
// Static assets (/_app/*.js, CSS) do NOT flow through this hook -- adapter-node
// serves them via sirv before the SvelteKit handler runs -- so they are handled
// separately by `precompress: true` in svelte.config.js (build-time gzip/brotli).
//
// A note on `reroute`: it lives in the universal hooks.js (runs on both server
// and client). `handle` is server-only and must live here.

import { Readable, pipeline } from "node:stream";
import zlib from "node:zlib";

// Below ~one MTU the framing + header overhead of compression outweighs the
// savings, so bodies with a known small length pass through.
const MIN_BYTES = 1400;

// Only text-shaped payloads. Images/fonts/video are already compressed;
// re-compressing wastes CPU and can grow the body. text/event-stream is
// deliberately excluded below (it is a live stream we must never buffer).
const COMPRESSIBLE =
  /^(?:text\/|application\/(?:json|javascript|xml|manifest\+json)|application\/[\w.+-]+\+(?:json|xml)|image\/svg\+xml)\b/i;

export function pickEncoding(accept) {
  if (!accept) return null;
  const qualities = new Map();
  for (const entry of accept.toLowerCase().split(",")) {
    const [name, ...parameters] = entry.trim().split(";");
    const quality = parameters.find((p) => p.trim().startsWith("q="));
    const value = quality ? Number(quality.trim().slice(2)) : 1;
    qualities.set(
      name.trim(),
      Number.isFinite(value) && value >= 0 && value <= 1 ? value : 0,
    );
  }
  const quality = (name) => qualities.get(name) ?? qualities.get("*") ?? 0;
  const br = quality("br");
  const gzip = quality("gzip");
  // Identity is acceptable by default, but does not override an offered encoding.
  const identity =
    qualities.get("identity") ?? (qualities.get("*") === 0 ? 0 : null);
  const best = Math.max(br, gzip);
  if (best === 0 || (identity != null && identity > best)) return null;
  return br >= gzip ? "br" : "gzip";
}

/** @type {import('@sveltejs/kit').Handle} */
export async function handle({ event, resolve }) {
  const response = await resolve(event);

  // The Activity index still accepts legacy JSON clients at its HTML URL.
  // Cloudflare's URL cache key does not distinguish Accept, so neither
  // representation can be stored here. Keep the exception at the response
  // boundary to cover HTML, JSON, navigation data, HEAD and error responses.
  // Cacheable JSON lives at /slop/factory/data/activity (agent aggregates)
  // and /slop/factory/data/board (the factory ledger).
  if (event.route?.id === "/public/slop/factory/activity") {
    response.headers.set("cache-control", "no-store");
    response.headers.set("cloudflare-cdn-cache-control", "no-store");
    // Do not revalidate a previously cached representation into a 304. Kit
    // evaluates If-None-Match after this hook, so remove its validator too.
    response.headers.delete("etag");
  }

  // Campaign pages contain private roster data and one-time invitation action
  // results. Apply at the response boundary so action failures, navigation
  // payloads and redirects cannot be cached even when no page load runs.
  if (
    event.route?.id === "/friends/grimoire" ||
    event.route?.id?.startsWith("/friends/grimoire/")
  ) {
    response.headers.set("cache-control", "private, no-store");
    response.headers.set("cloudflare-cdn-cache-control", "no-store");
    // Native form POSTs need their same-origin Origin for CSRF checks.
    // no-referrer makes navigation POST Origin null (Fetch section 3.2).
    // same-origin still omits referrers to other origins; invitation fragments
    // are cleared by the standalone landing before it sends any request.
    response.headers.set("referrer-policy", "same-origin");
    response.headers.delete("etag");
  }

  // HEAD must not have its body materialised; pass through untouched.
  if (event.request.method === "HEAD" || !response.body) return response;
  // Respect anything upstream already encoded.
  if (response.headers.has("content-encoding")) return response;

  const type = response.headers.get("content-type") || "";
  // Server-sent events are an open stream: buffering it (below) would stall the
  // chat transcript forever. Never touch it.
  if (type.startsWith("text/event-stream")) return response;
  if (!COMPRESSIBLE.test(type)) return response;

  const encoding = pickEncoding(event.request.headers.get("accept-encoding"));
  if (!encoding) return response;

  // Fast path: a known-small body never needs buffering at all.
  const declaredLen = Number(response.headers.get("content-length"));
  if (declaredLen && declaredLen < MIN_BYTES) return response;

  // Stream through the compressor with backpressure. Buffering every complete
  // response here multiplies memory during concurrent SSR/API requests.
  // Unknown-length bodies are compressed even when small so finding their size
  // never requires retaining the full response.
  const compressor =
    encoding === "br"
      ? zlib.createBrotliCompress({
          params: { [zlib.constants.BROTLI_PARAM_QUALITY]: 5 },
        })
      : zlib.createGzip({ level: 6 });
  const compressed = Readable.toWeb(compressor, {
    strategy: {
      highWaterMark: 64 * 1024,
      size: (chunk) => chunk.byteLength,
    },
  });
  // pipeline propagates source errors and destroys the source on downstream
  // cancellation. The response reader observes errors through toWeb; consume
  // the callback error so there is no unhandled background rejection.
  pipeline(Readable.fromWeb(response.body), compressor, () => {});

  const headers = new Headers(response.headers);
  headers.set("content-encoding", encoding);
  headers.delete("content-length");

  // Caches (Cloudflare) must key on accept-encoding so a brotli body is never
  // handed to a gzip-only client. Add the token without clobbering an existing
  // Vary or duplicating ourselves.
  const vary = headers.get("vary");
  if (!vary) {
    headers.set("vary", "Accept-Encoding");
  } else if (!/\baccept-encoding\b/i.test(vary)) {
    headers.set("vary", `${vary}, Accept-Encoding`);
  }

  return new Response(compressed, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}
