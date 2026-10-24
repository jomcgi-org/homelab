import { readable } from "svelte/store";
import pages from "./.generated/pages.json";
import post from "../preview/fixtures/blog-page.json";

// Invented records only. The production loaders and APIs are never imported.
const input = new URL(location.href).searchParams;
export const view = input.get("view") ?? "overview";
export const scenario = input.get("scenario") ?? "live";
const isUpdates = view.startsWith("updates");
const slug = view === "landing" ? post.slug : "synthetic-post";
export function fixturePath(surface) {
  if (surface.startsWith("updates"))
    return `/updates?month=2026-10${surface.includes("filter") ? "&project=monolith" : ""}`;
  if (surface.startsWith("blog")) return "/blog";
  if (["post", "single", "landing"].includes(surface))
    return `/blog/${surface === "landing" ? post.slug : "synthetic-post"}`;
  return (
    "/slop/factory" +
    ({
      context: "/context",
      chapter: "/context?entity=synthetic-project",
      search: "/context?q=Synthetic",
      activity: "/activity",
      task: "/activity/900001",
      attempt: "/activity/900001/implement/1",
      "work-item": "/work-items/900001",
    }[surface] ?? "")
  );
}
const path = fixturePath(view);
const long = "synthetic-unbroken-record-".repeat(12);
const section = `<h2 id="long-section">3. A long section</h2><p>${"An invented paragraph tests wrapping and document scrolling. ".repeat(120)}</p><p><a href="#long-section">${long}</a></p>`;
const blogPost = {
  ...post,
  slug,
  sections: [...post.sections, section],
  toc: [
    ...post.toc,
    { id: "long-section", text: "3. A long section", children: [] },
  ],
};
if (view === "single") blogPost.toc = [];
const posts = Array.from({ length: 6 }, (_, i) => ({
  ...post,
  slug: `synthetic-${i}`,
  title: `Synthetic record ${i + 1}: ${post.title}`,
}));
const update = (i) => ({
  published_on: `2026-10-${String(23 - i).padStart(2, "0")}`,
  category: "improvement",
  headline: `Synthetic edition ${i + 1}: bounded fixture records`,
  summary: "Invented journal data for stylesheet regression checks.",
  projects: ["monolith"],
  technologies: ["frontend"],
  source_compare_url: "https://example.invalid/compare",
  source_commit_count: 3,
  highlights: [
    {
      title: "Synthetic change",
      description: "An invented description. ".repeat(35),
    },
  ],
  improvements: [
    {
      title: "Synthetic supporting change",
      description: "A deterministic supporting entry.",
    },
  ],
});
const updates =
  view === "updates-empty" || view === "updates-filter-empty"
    ? []
    : Array.from({ length: 5 }, (_, i) => update(i));
const updateData = {
  updates,
  projects: [{ value: "monolith", count: 5 }],
  technologies: [{ value: "frontend", count: 5 }],
  selectedProject: view.includes("filter") ? "monolith" : "",
  selectedTechnology: "",
  selectedMonth: "2026-10",
  months: [
    { month: "2026-10", count: 5, editions: updates },
    {
      month: "2026-09",
      count: 2,
      editions: [{ ...update(0), published_on: "2026-09-23" }],
    },
  ],
  error: false,
  invalidMonth: false,
};
export const turn = {
  seq: 1,
  prompt: "Inspect an invented record. " + long,
  result_text:
    "## Synthetic result\n\n" + "Invented transcript content. ".repeat(230),
  activities: [],
  cost_usd: 0.01,
  diff: "diff --git a/synthetic.txt b/synthetic.txt\n--- a/synthetic.txt\n+++ b/synthetic.txt\n@@ -1 +1 @@\n-before\n+after\n",
};
export const attempt = {
  attempt: 1,
  status: "succeeded",
  cost_usd: 0.01,
  session_key: "synthetic-session",
  turns: [turn],
};
export const node = {
  node_key: "implement",
  state: "done",
  model: "synthetic",
  depends_on: [],
  attempts: [attempt],
};
export const task = {
  issue_number: 900001,
  title: "Synthetic task detail: an invented transcript and plan",
  state: "landed",
  task_class: "delivery",
  generation: 1,
  phase: "complete",
  nodes: [node],
  brief: ["An invented brief."],
  turns_used: 1,
  cost_usd: 0.01,
  admitted_at: "2026-10-03T11:00:00Z",
  finished_at: "2026-10-03T12:00:00Z",
  url: "https://example.invalid/issue",
};
const detail = {
  task,
  node,
  attempt,
  policy: { max_attempts: 3 },
  session: { model: "synthetic", status: "succeeded", turn_count: 1 },
  turns: [turn],
  snapshottedAt: "2026-10-03T12:00:00Z",
};
const work = {
  document: {
    item: {
      id: 900001,
      title: "Synthetic work item",
      source_ref: "https://example.invalid/issue",
      github_issue_number: 900001,
      state: "open",
      authority: "synthetic",
      trust: "invented",
      labels: [],
    },
    edges_in: [{ kind: "blocks", from_id: 900002, source: "synthetic" }],
    edges_out: [],
  },
};
export function fixtureData(surface, mode = scenario) {
  if (surface.startsWith("updates")) {
    const empty = surface.includes("empty");
    return {
      ...updateData,
      updates: empty ? [] : Array.from({ length: 5 }, (_, i) => update(i)),
      selectedProject: surface.includes("filter") ? "monolith" : "",
      months: surface === "updates-empty" ? [] : updateData.months,
    };
  }
  if (surface.startsWith("blog"))
    return {
      posts: surface === "blog-empty" ? [] : posts,
      months:
        surface === "blog-empty" ? [] : [{ label: "January 2026", posts }],
    };
  if (["post", "single", "landing"].includes(surface))
    return {
      ...blogPost,
      slug: surface === "landing" ? post.slug : "synthetic-post",
      toc:
        surface === "single"
          ? []
          : [
              ...post.toc,
              { id: "long-section", text: "3. A long section", children: [] },
            ],
    };
  return surface === "work-item"
    ? work
    : ["task", "attempt"].includes(surface)
      ? detail
      : pages[`${mode}/${surface}`];
}
export const data = fixtureData(view);
export const page = $state({
  params: {
    slug:
      path.includes("synthetic-post") || view === "landing" ? slug : undefined,
  },
  data,
  state: {},
  url: new URL(
    path,
    isUpdates ? "https://private.fixture.invalid" : "https://fixture.invalid",
  ),
});
export const storesPage = readable(page);
export const banner = input.has("banner")
  ? "Synthetic maintenance notice for layout regression checks."
  : "";
