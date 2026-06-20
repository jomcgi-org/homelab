// Server-only imgproxy URL signer for the /app/trips pages. Lives under
// $lib/server/** so SvelteKit guarantees it is NEVER bundled into the client:
// the HMAC signing secret (IMGPROXY_KEY / IMGPROXY_SALT) must stay server-side.
// The SSR loads pre-sign every image URL and pass the result as plain data, so
// the browser only ever sees a finished `/img/<signature>/<preset>/plain/...`
// URL and never the secret.
//
// imgproxy (monolith-public chart) runs with IMGPROXY_PATH_PREFIX=/img and, once
// IMGPROXY_KEY/IMGPROXY_SALT are set, REQUIRES a valid signature on every
// request (the old unsigned /unsafe/ path is retired). The signature is an
// HMAC-SHA256 over the request path AFTER the /img prefix (imgproxy strips the
// prefix before checking the signature).
import { createHmac } from "node:crypto";
import { env } from "$env/dynamic/private";
import { PRESETS } from "../trips/images.js";

const IMG_BASE = "/img";
const S3_PREFIX = "s3://monolith-trips/";

let warned = false;
function warnUnsigned() {
  if (warned) return;
  warned = true;
  // Local dev without the secret: fall back to unsigned URLs so the pages still
  // render. In prod IMGPROXY_KEY/SALT are always injected (secretKeyRef), so
  // this branch never runs there.
  console.warn(
    "[trips-img] IMGPROXY_KEY/IMGPROXY_SALT unset, falling back to unsigned /img/unsafe/ URLs (local dev only).",
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

// Build the signed (or, without a secret, unsigned) same-origin imgproxy URL for
// one object key + named preset. `path` is everything after the signature and is
// exactly what imgproxy signs once it has stripped the /img prefix.
function build(key, preset) {
  const p = PRESETS.has(preset) ? preset : "gallery";
  const path = `/${p}/plain/${S3_PREFIX}${key}`;
  if (!env.IMGPROXY_KEY || !env.IMGPROXY_SALT) {
    warnUnsigned();
    return `${IMG_BASE}/unsafe${path}`;
  }
  return `${IMG_BASE}/${sign(path)}${path}`;
}

// key is the TripPoint `image` value (the S3 object key for that photo).
export function signedImgUrl(key, preset = "gallery") {
  return build(key, preset);
}

export function signedFullUrl(key) {
  return build(key, "full");
}
