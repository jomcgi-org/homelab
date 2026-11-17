import { readFileSync } from "node:fs";
import type { Policy } from "./ScotlandPolicyPage";
import { describe, expect, it } from "vitest";
import { privacy, terms, support, UPDATED } from "./policy-data";
import { render } from "./prerender";
import { facts, setups, sources } from "./scotland-data";
import { SECTIONS, sectionsIn } from "./sections";

// These original files are test-only deps in BUILD. The production site does
// not import the hub or depend on its Svelte runtime.
const policies: Policy[] = [privacy, terms, support];
const original = (path: string) =>
  readFileSync(`../../monolith/frontend/src/routes/public/${path}`, "utf8");
const normalized = (text: string) => text.replace(/\s+/g, " ").trim();
const body = (entry: string) => {
  const element = document.createElement("div");
  element.innerHTML = render(entry);
  return element;
};

describe("Scotland and slop migration", () => {
  it.each(["scotland-data", "policy-data"])(
    "%s keeps the hub's committed content verbatim",
    (name) => {
      const withoutImports = (text: string) =>
        text
          .replace(/^import[^;]+;\n/gm, "")
          .replace(/^\/\/.*\n/gm, "")
          .trim();
      expect(withoutImports(readFileSync(`src/${name}.ts`, "utf8"))).toBe(
        withoutImports(original(`scotland/${name}.js`)),
      );
    },
  );

  it.each(["scotland", "slop"])("%s preserves the page's prose", (name) => {
    const source = original(`${name}/+page.svelte`);
    const content = normalized(body(`${name}.html`).textContent!);
    const paragraphs = [
      ...source.matchAll(/<(p|h[1-3])\b[^>]*>([^<{]+)<\/\1>/g),
    ];
    expect(paragraphs.length).toBeGreaterThan(2);
    for (const [, , text] of paragraphs)
      expect(content).toContain(normalized(text!));
  });

  it("renders all connector facts, commands and source credits before JavaScript", () => {
    const page = body("scotland.html");
    const content = normalized(page.textContent!);
    for (const fact of facts) {
      expect(content).toContain(fact.k);
      expect(content).toContain(fact.v);
    }
    for (const setup of setups) {
      expect(content).toContain(setup.label);
      expect(content).toContain(setup.how);
      if (setup.code)
        expect(
          [...page.querySelectorAll("pre")].map((pre) => pre.textContent),
        ).toContain(setup.code);
    }
    for (const source of sources) {
      for (const value of Object.values(source))
        expect(content).toContain(value);
    }
    expect(
      page.querySelector('a[href="https://jomcgi.dev/app/stars"]'),
    ).toBeTruthy();
  });

  it.each(policies)("renders every $slug paragraph and link", (doc) => {
    const page = body(`scotland/${doc.slug}.html`);
    const content = normalized(page.textContent!);
    expect(content).toContain(`updated ${UPDATED}`);
    expect(content).toContain(doc.lead);
    for (const section of doc.sections) {
      expect(content).toContain(section.heading);
      for (const paragraph of section.paras)
        expect(content).toContain(paragraph);
      if (section.rows) {
        for (const row of section.rows ?? []) {
          expect(page.querySelector(`a[href="${row.href}"]`)?.textContent).toBe(
            row.v,
          );
        }
      }
    }
  });

  const metadata = [
    ...["scotland", "slop"].map((slug) => {
      const source = original(`${slug}/+page.svelte`);
      return {
        slug,
        title: source.match(/title="([^"]+)"/)![1]!,
        description: source.match(/description="([^"]+)"/)![1]!,
      };
    }),
    ...[privacy, terms, support].map((doc) => ({
      slug: `scotland/${doc.slug}`,
      title: `${doc.title} · Scotland connector · jomcgi.dev`,
      description: doc.description,
    })),
  ];
  it.each(metadata)(
    "$slug preserves the hub's SEO tags in its HTML entry",
    ({ slug, title, description }) => {
      const doc = new DOMParser().parseFromString(
        readFileSync(`${slug}.html`, "utf8"),
        "text/html",
      );
      const meta = (key: string) =>
        doc
          .querySelector(`meta[name="${key}"], meta[property="${key}"]`)
          ?.getAttribute("content");
      expect(doc.title).toBe(title);
      expect(meta("description")).toBe(description);
      expect(
        doc.querySelector('link[rel="canonical"]')?.getAttribute("href"),
      ).toBe(`https://jomcgi.dev/${slug}`);
      expect(meta("og:type")).toBe("website");
      expect(meta("og:site_name")).toBe("Joe McGinley");
      expect(meta("og:title")).toBe(title);
      expect(meta("og:description")).toBe(description);
      expect(meta("og:url")).toBe(`https://jomcgi.dev/${slug}`);
      expect(meta("twitter:card")).toBe("summary");
      expect(meta("twitter:title")).toBe(title);
      expect(meta("twitter:description")).toBe(description);
    },
  );

  it("keeps slop live, unlisted, and noindex in both HTML and HTTP headers", () => {
    expect(SECTIONS.find((section) => section.path === "/slop")).toMatchObject({
      live: true,
      listed: false,
    });
    expect(SECTIONS.find((section) => section.path === "/scotland")?.live).toBe(
      true,
    );
    expect(
      sectionsIn("Writing").some((section) => section.path === "/slop"),
    ).toBe(false);
    const html = readFileSync("slop.html", "utf8");
    expect(html).toContain('<meta name="robots" content="noindex, nofollow"');
    expect(readFileSync("public/_headers", "utf8")).toMatch(
      /\n\/slop\n\s+X-Robots-Tag: noindex, nofollow/,
    );
    expect(body("index.html").querySelector('a[href="/slop"]')).toBeNull();
  });
});
