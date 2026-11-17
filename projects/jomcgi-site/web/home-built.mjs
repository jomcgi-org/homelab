// HOME-206: verify the emitted home without executing its scripts.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { Window } from "happy-dom";
import { hub, latestPosts, posts } from "./site-inputs.mjs";
import { robots, sitemap, llms, webmanifest } from "./machine-readable.mjs";
import { PUBLIC_BASE } from "../../monolith/frontend/src/lib/public/seo.js";

export function checkHome(dist) {
  const read = (file) => readFileSync(join(dist, file));
  const original = (file) => readFileSync(new URL(file, hub));
  const html = read("index.html").toString();
  const document = new Window().document;
  document.body.innerHTML = html;
  const source = original("src/routes/public/+page.svelte").toString();
  const find = (selector) => document.querySelector(selector);
  const all = (selector) => [...document.querySelectorAll(selector)];
  const attr = (selector, name) => find(selector)?.getAttribute(name);
  const text = (value) =>
    value
      .replace(/<[^>]+>/g, " ")
      .replace(/\s+/g, " ")
      .trim();
  const [, title, description] = source.match(
    /<Seo\s+title="([^"]+)"\s+description="([^"]+)"/,
  );
  assert.equal(document.title, title);
  const canonical = `${PUBLIC_BASE}/`;
  assert.equal(attr('[rel="canonical"]', "href"), canonical);
  for (const [key, value] of Object.entries({
    description,
    "og:description": description,
    "twitter:description": description,
    "og:title": title,
    "twitter:title": title,
    "og:url": canonical,
    "og:type": "website",
    "og:site_name": "Joe McGinley",
    "twitter:card": "summary",
  })) {
    assert.equal(
      attr(`meta[name="${key}"],meta[property="${key}"]`, "content"),
      value,
      key,
    );
  }
  assert.equal(
    text(find("h1").textContent),
    text(source.match(/<h1[^>]*>([^{]+)/)[1]),
  );
  assert.equal(
    text(find(".glass .small").textContent),
    text(source.match(/<p class="small">\s*(Building[\s\S]*?)<\/p>/)[1]),
  );
  assert.deepEqual(
    [...find(".unit").children].map((node) => node.className),
    ["plate", "glass", "deck", "foot"],
  );
  assert.equal(find(".brand").textContent, "jomcgi.dev");
  assert.equal(find(".meta b").textContent, "--:--:--");
  const order = JSON.parse(source.match(/const ORDER = (\[[^;]+\]);/)[1]);
  const legends = [...source.matchAll(/legend: "([^"]+)"/g)].map(
    (match) => match[1],
  );
  assert.deepEqual(
    all(".pad .legend").map((node) => node.textContent),
    [...legends, ...order.filter((slug) => slug !== "notes")],
  );
  assert.deepEqual(
    all(".writing a").map((node) => [
      node.querySelector("time").textContent,
      node.querySelector("span").textContent,
      new URL(node.getAttribute("href"), canonical).pathname,
    ]),
    latestPosts(posts).map((post) => [
      post.date,
      post.title,
      `/blog/${post.slug}`,
    ]),
  );
  const portrait = attr(".portrait-still", "src");
  assert.deepEqual(read(portrait.slice(1)), original("static/joe-grey.png"));
  assert.equal(all("canvas").length, 1);
  assert.equal(all("[style]").length, 0);
  assert.equal(find('a[href*="notes"]'), null);
  for (const [file, expected] of Object.entries({
    "robots.txt": robots(),
    "sitemap.xml": sitemap(posts),
    "llms.txt": llms(posts),
    "manifest.webmanifest": JSON.stringify(webmanifest()),
  }))
    assert.equal(read(file).toString(), expected, file);
  for (const file of ["favicon.svg", "apple-touch-icon.png"]) {
    assert.deepEqual(read(file), original(`static/${file}`), file);
  }
  assert.deepEqual(read("assets/OFL.txt"), original("preview/fonts/OFL.txt"));
}
