// /docs overview. The tree itself comes from the layout's `sidebar`; this load
// only sets the cache header and page meta. No manifest body crosses the wire.
import { DOCS_CACHE_CONTROL } from "$lib/cache-headers.js";

export function load({ setHeaders }) {
  setHeaders({ "cache-control": DOCS_CACHE_CONTROL });
  return {
    meta: {
      title: "Documentation",
      description:
        "Project READMEs and architecture decision records for the homelab platform.",
    },
  };
}
