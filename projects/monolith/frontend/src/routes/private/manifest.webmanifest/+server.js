import { manifestResponse } from "$lib/manifest.js";

export const prerender = false;

// Behind Cloudflare Access: the browser may keep it, a shared cache may not.
export function GET() {
  return manifestResponse("private.jomcgi.dev", "dashboard", {
    "cache-control": "private, max-age=600",
  });
}
