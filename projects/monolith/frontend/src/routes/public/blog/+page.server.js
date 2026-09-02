import { version } from "$app/environment";
import {
  cloudflareCacheHeaders,
  DOCS_CACHE_CONTROL,
} from "$lib/cache-headers.js";
// Server-only import: post bodies stay out of the client bundle.
import manifest from "$lib/public/posts/posts-manifest.json";
import { groupByMonth } from "./blog.js";

export function load({ setHeaders, url }) {
  const selectedTag = url.searchParams.get("tag") || "";
  const allPosts = manifest.map(({ slug, title, date, summary, tags }) => ({
    slug,
    title,
    date,
    summary,
    tags,
  }));
  const counts = new Map();
  for (const post of allPosts) {
    for (const tag of post.tags) counts.set(tag, (counts.get(tag) || 0) + 1);
  }
  const tags = [...counts.entries()]
    .map(([name, count]) => ({ name, count }))
    .sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));
  const posts = selectedTag
    ? allPosts.filter((post) => post.tags.includes(selectedTag))
    : allPosts;

  setHeaders({
    ...cloudflareCacheHeaders(DOCS_CACHE_CONTROL),
    etag: `"${version}-blog-${selectedTag || "all"}"`,
  });

  return {
    posts,
    tags,
    selectedTag,
    months: groupByMonth(posts),
  };
}
