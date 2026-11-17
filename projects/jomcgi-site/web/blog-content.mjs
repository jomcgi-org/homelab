// The index receives only public summaries. Markdown and recordings stay out
// of both the page HTML and its browser bundle.
import { readFileSync } from "node:fs";

const manifest = JSON.parse(
  readFileSync(
    new URL(
      "../../monolith/frontend/src/lib/public/posts/posts-manifest.json",
      import.meta.url,
    ),
    "utf8",
  ),
);
export const summaries = manifest.map(
  ({ slug, title, summary, date, tags }) => ({
    slug,
    title,
    summary,
    date,
    tags,
  }),
);

export function blogContent() {
  const id = "virtual:blog/index";
  return {
    name: "blog-index",
    resolveId(source) {
      if (source === id) return `\0${id}`;
    },
    load(source) {
      if (source === `\0${id}`)
        return `export default ${JSON.stringify(summaries)}`;
    },
  };
}
