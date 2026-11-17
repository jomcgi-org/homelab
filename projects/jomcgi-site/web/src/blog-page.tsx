import { useEffect, useState } from "react";
import posts from "virtual:blog/index";
import { Shell } from "./Shell";
import { ORIGIN } from "./sections";
import { Tags, formatDate } from "./blog-shared";
import "./blog.css";

const counts = new Map<string, number>();
for (const post of posts)
  for (const tag of post.tags) counts.set(tag, (counts.get(tag) ?? 0) + 1);
const tags = [...counts].sort(
  (a, b) => b[1] - a[1] || a[0].localeCompare(b[0]),
);
function selectedTag() {
  const tag = new URLSearchParams(window.location.search).get("tag") ?? "";
  return /^[a-z0-9][a-z0-9-]{0,23}$/.test(tag) ? tag : "";
}
export function BlogPage() {
  const [selected, setSelected] = useState("");
  useEffect(() => {
    const update = () => setSelected(selectedTag());
    update();
    window.addEventListener("popstate", update);
    return () => window.removeEventListener("popstate", update);
  }, []);
  const visible = selected
    ? posts.filter((post) => post.tags.includes(selected))
    : posts;
  return (
    <Shell>
      <div className="blog">
        <h1>Blog</h1>
        <nav aria-label="Filter by tag">
          <ul className="blog-tags">
            {[["", posts.length] as const, ...tags].map(([tag, count]) => (
              <li key={tag}>
                <a
                  href={tag ? `/blog?tag=${tag}` : "/blog"}
                  aria-current={selected === tag ? "page" : undefined}
                  onClick={(event) => {
                    if (
                      event.button !== 0 ||
                      event.metaKey ||
                      event.ctrlKey ||
                      event.shiftKey ||
                      event.altKey
                    )
                      return;
                    event.preventDefault();
                    window.history.pushState(
                      null,
                      "",
                      event.currentTarget.href,
                    );
                    setSelected(tag);
                  }}
                >
                  {tag || "All"} ({count})
                </a>
              </li>
            ))}
          </ul>
        </nav>
        <section aria-label="Blog posts">
          {visible.length ? (
            visible.map((post) => (
              <article key={post.slug}>
                <time dateTime={post.date}>{formatDate(post.date)}</time>
                <h2>
                  <a href={`${ORIGIN}/blog/${post.slug}`}>{post.title}</a>
                </h2>
                <p>{post.summary}</p>
                <Tags tags={post.tags} />
              </article>
            ))
          ) : (
            <h2>Nothing published yet.</h2>
          )}
        </section>
      </div>
    </Shell>
  );
}
