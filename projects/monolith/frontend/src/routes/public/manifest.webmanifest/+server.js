import {
  MANIFEST_CACHE_CONTROL,
  cloudflareCacheHeaders,
} from "$lib/cache-headers.js";
import { manifestResponse } from "$lib/manifest.js";

export const prerender = false;

export function GET() {
  return manifestResponse(
    "jomcgi.dev",
    "jomcgi",
    cloudflareCacheHeaders(MANIFEST_CACHE_CONTROL),
  );
}
