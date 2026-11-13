// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import { setCachedGraph } from "$lib/public/notes/graph-cache.js";
import PrivateRoute from "./+page.svelte";
import PublicRoute from "../../public/app/notes/+page.svelte";
vi.mock("$app/environment", () => ({ browser: true, version: "testbuild" }));
vi.mock("$app/state", () => ({
  page: { url: new URL("https://jomcgi.dev/app/notes?focus=note") },
}));
vi.mock("$lib/components/notes/KnowledgeGraph.svelte", async () => ({
  default: (await import("../../../../test/NotesGraph.svelte")).default,
}));
vi.mock("$lib/components/notes/GraphLegend.svelte", () => ({
  default: () => {},
}));
vi.mock("$lib/components/notes/GraphSearch.svelte", () => ({
  default: () => {},
}));
let component;
afterEach(async () => {
  if (component) await unmount(component);
  document.body.replaceChildren();
  setCachedGraph(null, new Headers());
  vi.unstubAllGlobals();
});
const graph = {
  nodes: [{ id: "note", title: "Note", type: "fact" }],
  edges: [],
};
for (const tier of ["private", "public"])
  for (const count of [0, 1, 3]) {
    it(`${tier} route renders ${count} stored attempts and deadline`, async () => {
      const entries = Array.from({ length: count }, (_, i) => ({
        id: i + 1,
        status: i === 2 ? "unavailable" : "success",
        attempted_at: "2026-10-20T12:00:00Z",
        note_revision: i ? 1 : 2,
        is_current_revision: i === 0,
        reviewer_lane: i ? null : "source-review",
        ...(tier === "private"
          ? {
              reason: "Private reason",
              next_attempt_at: "2026-10-21T12:00:00Z",
            }
          : {}),
      }));
      const fetcher = vi.fn(
        async (url) =>
          new Response(
            JSON.stringify(
              url === "/app/notes/graph"
                ? graph
                : {
                    body: "Stored body",
                    content: "Stored body",
                    review_after: "2099-10-30T12:00:00Z",
                    review_history: { entries, has_more: false },
                  },
            ),
            {
              headers: {
                "cache-control":
                  url === "/app/notes/graph"
                    ? "public, max-age=60"
                    : "no-store",
              },
            },
          ),
      );
      vi.stubGlobal("fetch", fetcher);
      const target = document.createElement("div");
      document.body.append(target);
      component = mount(tier === "private" ? PrivateRoute : PublicRoute, {
        target,
        props: tier === "private" ? { data: { graph } } : {},
      });
      await tick();
      if (tier === "private")
        [...target.querySelectorAll("button")]
          .find((b) => b.textContent.includes("Select Note"))
          .click();
      await vi.waitFor(() =>
        expect(target.textContent).toContain("Review history"),
      );
      const text = target.textContent.replace(/\s+/g, " ");
      expect(text).toContain("Next review deadline: 2099-10-30 12:00:00 UTC");
      expect(text).toContain(
        "A success for another revision does not confirm the current claim",
      );
      expect(target.querySelectorAll(".review-history li")).toHaveLength(count);
      if (!count) expect(text).toContain("No review attempts recorded");
      else expect(text).toContain("Reviewer/lane: source-review");
      if (count > 1) {
        expect(text).toContain("other claim revision");
        expect(text).toContain("Reviewer/lane: unrecorded");
      }
      expect(
        fetcher.mock.calls.some(
          ([url]) =>
            url ===
            (tier === "private"
              ? "/api/knowledge/notes/note?include_review_history=true"
              : "/app/notes/body/note"),
        ),
      ).toBe(true);
      if (tier === "public") {
        expect(text).not.toContain("Private reason");
        expect(text).not.toContain("Next attempt recorded");
      }
    });
  }
