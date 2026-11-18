import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { expect, it } from "vitest";
import { categories, intro, marqueeItems, projects } from "./engineering-data";
import { agentRestoreColdMs } from "./engineering-metrics";
import { render } from "./prerender";

// The original hub files are test inputs only. The static page's production
// build has no dependency on the hub, its Svelte runtime, or its trace data.
const hub = resolve("../../monolith/frontend/src/routes/public/engineering");
const read = (file: string) => readFileSync(resolve(hub, file), "utf8");

// A child Node process imports the hub module (an ES module) and prints its
// content exports as JSON. Vitest's runner cannot load a file outside the
// site's root in the Bazel sandbox, though the file is in the runfiles.
const hubExports = () =>
  JSON.parse(
    execFileSync(
      process.execPath,
      [
        "--input-type=module",
        "--eval",
        `const m = await import(${JSON.stringify(
          pathToFileURL(resolve(hub, "engineering-data.js")).href,
        )});
process.stdout.write(JSON.stringify({ categories: m.categories, intro: m.intro, marqueeItems: m.marqueeItems, projects: m.projects }));`,
      ],
      { encoding: "utf8" },
    ),
  );
const asJson = (value: unknown) => JSON.parse(JSON.stringify(value));

it("copies every hub content export unchanged", () => {
  expect(asJson({ categories, intro, marqueeItems, projects })).toEqual(
    hubExports(),
  );
});

it("ports every hub diagram's boxes, groups and arrow labels into the pre-render", () => {
  const html = new DOMParser().parseFromString(
    render("engineering.html"),
    "text/html",
  );
  const registry = read("diagrams/index.js");
  for (const project of projects) {
    const component = registry.match(
      new RegExp(`(?:"${project.id}"|${project.id}): (\\w+)`),
    )![1];
    const original = read(`diagrams/${component}.svelte`);
    const figure = html.getElementById(project.id)!.querySelector("figure")!;
    const boxes = [
      ...original.matchAll(/<DBox\b([^>]+)>([\s\S]*?)<\/DBox\s*>/g),
    ].map(([, attrs, label]) => ({
      label: label!.replace(/\s+/g, " ").trim(),
      sub: attrs!.match(/sub="([^"]+)"/)?.[1] ?? "",
      external: attrs!.includes('role="external"'),
    }));
    expect(
      [...figure.querySelectorAll(".dbox")].map((box) => {
        const sub = box.querySelector(".dbox-sub")?.textContent ?? "";
        return {
          label: box.firstChild!.textContent,
          sub,
          external: box.classList.contains("external"),
        };
      }),
      project.id,
    ).toEqual(boxes);
    const groups = [...original.matchAll(/<DGroup label="([^"]+)"/g)].map(
      ([, label]) => label,
    );
    expect(
      [...figure.querySelectorAll(".dgroup-label")].map(
        (group) => group.textContent,
      ),
      project.id,
    ).toEqual(groups);
    const arrows = [...original.matchAll(/<DArrow\b([^>]+)\/>/g)].map(
      ([, attrs]) =>
        (attrs!.match(/label="([^"]+)"/)?.[1] ?? "").replace(
          "{agentRestoreColdMs}",
          String(agentRestoreColdMs),
        ),
    );
    expect(
      [...figure.querySelectorAll(".darrow-label")].map(
        (arrow) => arrow.textContent,
      ),
      project.id,
    ).toEqual(arrows);
  }
});

it("keeps the hub page title and description in the HTML entry", () => {
  const original = read("+page.svelte");
  const seo = original.match(/<Seo\s+([\s\S]*?)\/>/)![1]!;
  const html = new DOMParser().parseFromString(
    readFileSync("engineering.html", "utf8"),
    "text/html",
  );
  expect(html.title).toBe(seo.match(/title="([^"]+)"/)![1]);
  expect(
    html.querySelector('meta[name="description"]')!.getAttribute("content"),
  ).toBe(seo.match(/description="([^"]+)"/)![1]);
});
