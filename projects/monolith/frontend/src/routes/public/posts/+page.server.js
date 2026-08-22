import { version } from "$app/environment";
import { DOCS_CACHE_CONTROL } from "$lib/cache-headers.js";
// Server-only import: post bodies stay out of the client bundle.
import manifest from "$lib/public/posts/posts-manifest.json";

export function load({ setHeaders }) {
  setHeaders({
    "cache-control": DOCS_CACHE_CONTROL,
    etag: `"${version}-posts"`,
  });

  return {
    posts: manifest.map(({ slug, title, date, summary }) => ({
      slug,
      title,
      date,
      summary,
    })),
  };
}
