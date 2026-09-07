import { version } from "$app/environment";
import {
  cloudflareCacheHeaders,
  DOCS_CACHE_CONTROL,
} from "$lib/cache-headers.js";

// Restate the default so this lane is not prerendered if a parent enables it.
export const prerender = false;

export function load({ setHeaders }) {
  setHeaders({
    ...cloudflareCacheHeaders(DOCS_CACHE_CONTROL),
    etag: `"${version}-slop"`,
  });

  return {};
}
