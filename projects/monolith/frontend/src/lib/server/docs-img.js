// Server-only imgproxy URL signer for images referenced by repo READMEs on
// the public docs site. Same signing scheme as trips-img.js (HMAC-SHA256 over
// the path after the /img prefix), different source bucket: docs images are
// uploaded out-of-band to s3://docs-assets/ keyed by their REPO PATH, so a
// README's relative image ref resolves mechanically to its object key. The
// `full` preset is quality-only; SVG in -> SVG out is an imgproxy passthrough.
import { createHmac } from "node:crypto";
import { env } from "$env/dynamic/private";

const IMG_BASE = "/img";
const S3_PREFIX = "s3://docs-assets/";

let warned = false;
function warnUnsigned() {
  if (warned) return;
  warned = true;
  console.warn(
    "[docs-img] IMGPROXY_KEY/IMGPROXY_SALT unset, falling back to unsigned /img/unsafe/ URLs (local dev only).",
  );
}

function sign(path) {
  const h = createHmac("sha256", Buffer.from(env.IMGPROXY_KEY, "hex"));
  h.update(Buffer.from(env.IMGPROXY_SALT, "hex"));
  h.update(path);
  return h.digest("base64url");
}

// repoPath is the repository-relative path of the image (also its S3 key).
export function signedDocsImgUrl(repoPath) {
  const path = `/full/plain/${S3_PREFIX}${repoPath}`;
  if (!env.IMGPROXY_KEY || !env.IMGPROXY_SALT) {
    warnUnsigned();
    return `${IMG_BASE}/unsafe${path}`;
  }
  return `${IMG_BASE}/${sign(path)}${path}`;
}
