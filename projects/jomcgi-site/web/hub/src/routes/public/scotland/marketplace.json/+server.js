import {
  cloudflareCacheHeaders,
  MARKETPLACE_CACHE_CONTROL,
} from "$lib/cache-headers.js";
import { marketplace } from "../scotland-data.js";

// /scotland/marketplace.json: the Claude Code plugin marketplace for the
// Scotland connector, served as a file so `/plugin marketplace add` fetches
// this catalog instead of cloning the homelab monorepo.
export function GET() {
  return new Response(`${JSON.stringify(marketplace, null, 2)}\n`, {
    headers: {
      "content-type": "application/json; charset=utf-8",
      ...cloudflareCacheHeaders(MARKETPLACE_CACHE_CONTROL),
    },
  });
}
