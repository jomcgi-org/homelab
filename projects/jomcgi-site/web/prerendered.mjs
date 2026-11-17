// Fails when a built page was not pre-rendered: its marker is still there, or
// its root holds no markup. Search engines and link previews read this HTML.
// Usage: node prerendered.mjs <dist directory>
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { htmlPages } from "./budget.mjs";
import assert from "node:assert/strict";

/** True when the page's root element holds rendered markup and no marker. */
export function prerendered(html) {
  return !html.includes("<!--ssr-->") && /<div id="root">\s*<\w/.test(html);
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const dist = process.argv[2];
  let failed = false;
  for (const page of htmlPages(dist)) {
    const ok = prerendered(readFileSync(join(dist, page), "utf8"));
    console.log(`${ok ? "ok  " : "FAIL"}  ${page}`);
    failed ||= !ok;
  }
  // Check the shipped HTML, not only the React renderer. A missing Vite input
  // or an unregistered post must fail even when the other pages still build.
  const { Window } = await import("happy-dom");
  const window = new Window();
  const document = (entry) =>
    new window.DOMParser().parseFromString(
      readFileSync(join(dist, entry), "utf8"),
      "text/html",
    );
  const index = document("blog.html");
  const posts = JSON.parse(
    readFileSync(
      new URL(
        "../../monolith/frontend/src/lib/public/posts/posts-manifest.json",
        import.meta.url,
      ),
      "utf8",
    ),
  );
  for (const post of posts) {
    const entry = `blog/${post.slug}.html`;
    const doc = document(entry);
    assert.equal(doc.querySelector("h1")?.textContent, post.title, entry);
    assert.ok(index.querySelector(`a[href="/blog/${post.slug}"]`), entry);
    const headings = [...doc.querySelectorAll("h2, h3")].map(
      (h) => h.textContent,
    );
    for (const [, heading] of post.content.matchAll(/^#{2,3} (.+)$/gm))
      assert.ok(headings.includes(heading), `${entry}: missing ${heading}`);
    console.log(`ok    manifest title and sections: ${entry}`);
  }
  await window.happyDOM.close();
  process.exit(failed ? 1 : 0);
}
