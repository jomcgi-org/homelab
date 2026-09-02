// Server-only import: post bodies never enter the navigation client bundle.
import manifest from "$lib/public/posts/posts-manifest.json";

export function load() {
  return {
    maintenanceBanner: process.env.PUBLIC_MAINTENANCE_BANNER || "",
    hasBlog: manifest.length > 0,
  };
}
