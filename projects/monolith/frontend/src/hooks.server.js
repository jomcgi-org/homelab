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

import { promisify } from "node:util";
import zlib from "node:zlib";

const gzip = promisify(zlib.gzip);
const brotli = promisify(zlib.brotliCompress);

// Below ~one MTU the framing + header overhead of compression outweighs the
// savings, so small bodies (most API JSON, redirects, 304s) pass through.
const MIN_BYTES = 1400;

// Only text-shaped payloads. Images/fonts/video are already compressed;
// re-compressing wastes CPU and can grow the body. text/event-stream is
// deliberately excluded below (it is a live stream we must never buffer).
const COMPRESSIBLE =
  /^(?:text\/|application\/(?:json|javascript|xml|manifest\+json)|application\/[\w.+-]+\+(?:json|xml)|image\/svg\+xml)\b/i;

function pickEncoding(accept) {
  if (!accept) return null;
  const a = accept.toLowerCase();
  // Prefer brotli (better ratio on JSON, and the browser-preferred token) then
  // gzip. Anything else (identity only) means leave the body alone.
  if (a.includes("br")) return "br";
  if (a.includes("gzip")) return "gzip";
  return null;
}

/** @type {import('@sveltejs/kit').Handle} */
export async function handle({ event, resolve }) {
  const response = await resolve(event);

  // HEAD must not have its body materialised; pass through untouched.
  if (event.request.method === "HEAD") return response;
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

  // Buffer the body. Every browser-facing payload that reaches here is already
  // buffered upstream (json(await res.json()) for proxies, an SSR string for
  // pages), so this defeats no streaming we rely on -- and event-stream, the
  // one thing we DO stream, was excluded above.
  const original = new Uint8Array(await response.arrayBuffer());
  if (original.byteLength < MIN_BYTES) {
    // arrayBuffer() consumed the body; re-emit the small payload verbatim.
    return new Response(original, {
      status: response.status,
      statusText: response.statusText,
      headers: response.headers,
    });
  }

  const compressed =
    encoding === "br"
      ? await brotli(original, {
          params: {
            // Quality 5 lands near gzip-9's ratio at a fraction of brotli-11's
            // CPU. The async API runs on the libuv threadpool, so a big body
            // never blocks the Node event loop / other in-flight requests.
            [zlib.constants.BROTLI_PARAM_QUALITY]: 5,
            [zlib.constants.BROTLI_PARAM_SIZE_HINT]: original.byteLength,
          },
        })
      : await gzip(original, { level: 6 });

  const headers = new Headers(response.headers);
  headers.set("content-encoding", encoding);
  headers.set("content-length", String(compressed.byteLength));

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
