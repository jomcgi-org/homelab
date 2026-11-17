// HOME-198 and the home page use this published manifest; read it only at build time.
import { readFileSync } from "node:fs";
const moduleUrl = import.meta.url;
export const hub = new URL("../../monolith/frontend/", moduleUrl);
export const posts = JSON.parse(
  readFileSync(
    new URL("src/lib/public/posts/posts-manifest.json", hub),
    "utf8",
  ),
);
export function latestPosts(manifest = posts) {
  return [...manifest]
    .sort((a, b) => (a.date < b.date ? 1 : a.date > b.date ? -1 : 0))
    .slice(0, 2)
    .map(({ slug, title, date, summary }) => ({ slug, title, date, summary }));
}
export const homeDefines = { __HOME_POSTS__: JSON.stringify(latestPosts()) };
