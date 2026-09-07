import { version } from "$app/environment";
import {
  cloudflareCacheHeaders,
  DOCS_CACHE_CONTROL,
} from "$lib/cache-headers.js";

export const prerender = false;

export function load({ setHeaders }) {
  setHeaders({
    ...cloudflareCacheHeaders(DOCS_CACHE_CONTROL),
    etag: `"${version}-slop"`,
  });

  return {};
}
