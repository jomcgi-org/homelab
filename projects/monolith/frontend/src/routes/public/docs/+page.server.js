// /docs overview. Full manifest bodies remain server-only; cards receive only
// one short README excerpt and navigation metadata.
import { DOCS_CACHE_CONTROL } from "$lib/cache-headers.js";
import manifest from "$lib/public/docs/docs-manifest.json";
import { buildProjectCards } from "$lib/server/docs.js";

export function load({ setHeaders }) {
  setHeaders({ "cache-control": DOCS_CACHE_CONTROL });
  return {
    projects: buildProjectCards(manifest),
    meta: {
      title: "Documentation",
      description:
        "Current-state documentation for the public projects, rendered from the repository.",
    },
  };
}
