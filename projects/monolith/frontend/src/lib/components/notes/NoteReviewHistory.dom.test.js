// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from "vitest";
import { createRawSnippet, mount, tick, unmount } from "svelte";
import NoteReviewHistory from "./NoteReviewHistory.svelte";
import NotePanel from "./NotePanel.svelte";

const instances = [];
function render(component, props) {
  const target = document.createElement("div");
  document.body.append(target);
  instances.push(mount(component, { target, props }));
  return target;
}
const entry = (overrides = {}) => ({
  id: 1,
  status: "success",
  reason: "Verified stored evidence",
  reason_truncated: false,
  note_revision: 1,
  is_current_revision: true,
  attempted_at: "2026-10-20T12:00:00+00:00",
  evidence_observed_at: null,
  next_attempt_at: null,
  reviewer_lane: null,
  ...overrides,
});
afterEach(async () => {
  for (const instance of instances.splice(0)) await unmount(instance);
  document.body.replaceChildren();
  vi.unstubAllGlobals();
});
it("renders no attempts explicitly and missing projections silently", () => {
  const missing = render(NoteReviewHistory, {});
  expect(missing.textContent).toBe("");
  const empty = render(NoteReviewHistory, {
    history: { entries: [], has_more: false },
  });
  expect(empty.textContent).toContain("No review attempts recorded");
  expect(empty.querySelector("details").open).toBe(false);
});
it("labels stored status and revision without inventing reviewer attribution", () => {
  const root = render(NoteReviewHistory, {
    history: {
      entries: [
        entry(),
        entry({
          id: 2,
          status: "unsupported",
          note_revision: 0,
          is_current_revision: false,
          reason: "<script>bad()</script>",
          reason_truncated: true,
          evidence_observed_at: "2026-10-19T12:00:00+00:00",
          next_attempt_at: "2026-10-21T12:00:00+00:00",
        }),
        entry({ id: 3, status: "failed" }),
        entry({ id: 4, status: "unavailable" }),
      ],
      has_more: true,
    },
  });
  expect(root.querySelectorAll("li")).toHaveLength(4);
  expect(root.textContent).toContain("current claim");
  expect(root.textContent).toContain("other claim revision");
  expect(root.textContent).toContain("Reviewer/lane: unrecorded");
  expect(root.textContent).toContain("2026-10-20 12:00:00 UTC");
  expect(root.textContent).toContain(
    "Evidence observed: 2026-10-19 12:00:00 UTC",
  );
  expect(root.textContent).toContain(
    "Next attempt recorded: 2026-10-21 12:00:00 UTC",
  );
  expect(root.textContent).toContain(
    "Stored attempts. Current freshness is shown above.",
  );
  expect(root.textContent).toContain("Showing the newest 4 attempts");
  expect(root.textContent).toContain("(truncated)");
  expect(root.querySelector("script")).toBeNull();
});
it("keeps the shared public panel request and rendering free of history", async () => {
  const fetcher = vi.fn(
    async () =>
      new Response(
        JSON.stringify({
          body: "Public body",
          review_history: { entries: [entry()], has_more: false },
        }),
      ),
  );
  vi.stubGlobal("fetch", fetcher);
  const root = render(NotePanel, {
    selectedId: "public/note",
    nodes: [{ id: "public/note", title: "Public", type: "fact" }],
    edges: [],
    apiBase: "/app/notes/body",
  });
  await tick();
  await vi.waitFor(() => expect(root.textContent).toContain("Public body"));
  expect(fetcher.mock.calls[0][0]).toBe("/app/notes/body/public%2Fnote");
  expect(root.textContent).not.toContain("Review history");
});
it("requests the private expansion only through the explicit seam", async () => {
  const fetcher = vi.fn(
    async () =>
      new Response(
        JSON.stringify({
          content: "Private body",
          review_history: { entries: [entry()] },
        }),
      ),
  );
  vi.stubGlobal("fetch", fetcher);
  const detailExtra = createRawSnippet((getDetail) => ({
    render: () =>
      `<div data-history-count>${getDetail()?.review_history.entries.length ?? 0}</div>`,
  }));
  const root = render(NotePanel, {
    selectedId: "private/note",
    nodes: [{ id: "private/note", title: "Private", type: "fact" }],
    edges: [],
    detailQuery: "?include_review_history=true",
    detailExtra,
  });
  await tick();
  await vi.waitFor(() => expect(root.textContent).toContain("Private body"));
  expect(fetcher.mock.calls[0][0]).toBe(
    "/api/knowledge/notes/private%2Fnote?include_review_history=true",
  );
  expect(root.querySelector("[data-history-count]").textContent).toBe("1");
});
