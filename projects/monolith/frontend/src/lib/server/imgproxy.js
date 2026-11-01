// Keep signing credentials in $lib/server; callers send only finished URLs.
import { createHmac } from "node:crypto";
import { env } from "$env/dynamic/private";

const IMG_BASE = "/img";
const warnedLabels = new Set();

// key is an object key. Preserve its exact bytes in the path imgproxy signs.
export function signedUrl({ prefix, preset, presets, fallback, key, label }) {
  const p = presets.has(preset) ? preset : fallback;
  const path = `/${p}/plain/${prefix}${key}`;
  // Read dynamically on every call so credential rotation needs no re-import.
  const signingKey = env.IMGPROXY_KEY;
  const salt = env.IMGPROXY_SALT;
  if (!signingKey || !salt) {
    if (!warnedLabels.has(label)) {
      warnedLabels.add(label);
      console.warn(
        `[${label}] IMGPROXY_KEY/IMGPROXY_SALT unset, falling back to unsigned /img/unsafe/ URLs (local dev only).`,
      );
    }
    return `${IMG_BASE}/unsafe${path}`;
  }

  // Sign hex(SALT) ++ utf8(path), excluding /img; base64url omits padding.
  const h = createHmac("sha256", Buffer.from(signingKey, "hex"));
  h.update(Buffer.from(salt, "hex"));
  h.update(path);
  return `${IMG_BASE}/${h.digest("base64url")}${path}`;
}
