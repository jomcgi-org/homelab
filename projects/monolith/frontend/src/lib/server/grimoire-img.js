// Server-only imgproxy URL signer for the /app/grimoire continuous reader.
// Lives under $lib/server/** so SvelteKit guarantees it is NEVER bundled into
// the client: the HMAC signing secret (IMGPROXY_KEY / IMGPROXY_SALT) must stay
// server-side. Every book route's +page.server.js / read/+server.js pre-signs
// each chunk's image_key into an image_url before the response ever reaches
// the browser, so the browser only ever sees a finished
// `/img/<signature>/<preset>/plain/...` URL and never the secret or the raw
// bucket-relative key.
//
// Shares the exact HMAC scheme and imgproxy deployment as trips-img.js (same
// signing secret, same /img same-origin route on both hostnames): only the
// source bucket prefix and allowed preset names differ. Sourcebook
// illustrations live under s3://grimoire/books/ (see IMGPROXY_ALLOWED_SOURCES
// in monolith-public/chart/values.yaml).
import { createHmac } from "node:crypto";
import { env } from "$env/dynamic/private";

const IMG_BASE = "/img";
const S3_PREFIX = "s3://grimoire/";

// The reader only ever requests an inline display size or a full-quality zoom
// (see IMGPROXY_PRESETS in monolith-public/chart/values.yaml, shared with
// trips). "display" is the fallback for an unrecognized preset name.
const PRESETS = new Set(["display", "full"]);

let warned = false;
function warnUnsigned() {
  if (warned) return;
  warned = true;
  // Local dev without the secret: fall back to unsigned URLs so the pages still
  // render. In prod IMGPROXY_KEY/SALT are always injected (secretKeyRef), so
  // this branch never runs there.
  console.warn(
    "[grimoire-img] IMGPROXY_KEY/IMGPROXY_SALT unset, falling back to unsigned /img/unsafe/ URLs (local dev only).",
  );
}

// signature = base64url(HMAC_SHA256(key=hex(KEY), msg=hex(SALT) ++ utf8(path)))
// with no padding. Node's "base64url" digest encoding already omits padding.
function sign(path) {
  const h = createHmac("sha256", Buffer.from(env.IMGPROXY_KEY, "hex"));
  h.update(Buffer.from(env.IMGPROXY_SALT, "hex"));
  h.update(path);
  return h.digest("base64url");
}

// key is a KnowledgeChunk's bucket-relative image object key (e.g.
// "books/mm/raw/img/abc.jpg", as returned by /books/{id}/read's image_key).
export function signedGrimImgUrl(key, preset = "display") {
  const p = PRESETS.has(preset) ? preset : "display";
  const path = `/${p}/plain/${S3_PREFIX}${key}`;
  if (!env.IMGPROXY_KEY || !env.IMGPROXY_SALT) {
    warnUnsigned();
    return `${IMG_BASE}/unsafe${path}`;
  }
  return `${IMG_BASE}/${sign(path)}${path}`;
}
