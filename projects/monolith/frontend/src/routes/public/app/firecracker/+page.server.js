import { redirect } from "@sveltejs/kit";

// The firecracker explainer moved to /ember/firecracker alongside the rest of
// the ember demos. Keep this path alive as a permanent redirect so existing
// links (search engines, bookmarks, HomepageRack copy in old caches) still land.
export function load() {
  throw redirect(301, "/ember/firecracker");
}
