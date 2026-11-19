// The committed model-bench snapshot is frozen.
import leaderboard from "$lib/public/llm-leaderboard/leaderboard.json";
import {
  cloudflareCacheHeaders,
  PAGE_CACHE_CONTROL,
} from "$lib/cache-headers.js";

// Nothing purges Cloudflare on deploy, so a long edge TTL kept the previous
// snapshot live for up to a day after a republish. Rendering is a static import,
// so the shared 60s policy costs the origin nothing.
export function load({ setHeaders }) {
  setHeaders(cloudflareCacheHeaders(PAGE_CACHE_CONTROL));
  return { leaderboard };
}
