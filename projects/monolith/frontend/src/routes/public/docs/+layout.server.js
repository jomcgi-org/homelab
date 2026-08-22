// Server-only: the manifest (full doc bodies) is imported here and never
// reaches the client. buildSidebar returns project/tab metadata only, so the `sidebar`
// that crosses to the browser is small and body-free. Both the index page and
// every [...slug] page inherit it via the merged `data` prop.
import manifest from "$lib/public/docs/docs-manifest.json";
import { buildSidebar } from "$lib/server/docs.js";

export function load() {
  return { sidebar: buildSidebar(manifest) };
}
