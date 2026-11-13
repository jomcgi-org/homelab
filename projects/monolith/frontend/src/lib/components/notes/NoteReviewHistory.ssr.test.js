import { expect, it } from "vitest";
import { render } from "svelte/server";
import NoteReviewHistory from "./NoteReviewHistory.svelte";

it("renders a private recorded attempt without browser APIs or inferred success", async () => {
  const { html } = await render(NoteReviewHistory, {
    props: {
      history: {
        entries: [
          {
            id: 1,
            status: "unavailable",
            reason: "Source unavailable",
            note_revision: 0,
            is_current_revision: false,
            attempted_at: "2026-10-20T12:00:00+00:00",
          },
        ],
        has_more: false,
      },
    },
  });
  expect(html).toContain("unavailable");
  expect(html).toContain("other claim revision");
  expect(html).toContain("unrecorded");
  expect(html).not.toContain("<strong>success</strong>");
});

it("renders safe public metadata without private fields", async () => {
  const { html } = await render(NoteReviewHistory, {
    props: {
      reviewAfter: "2026-10-30T12:00:00Z",
      history: {
        entries: [
          {
            id: 1,
            status: "success",
            attempted_at: "2026-10-20T12:00:00Z",
            note_revision: 0,
            is_current_revision: false,
            reviewer_lane: "deterministic-verifier",
          },
        ],
        has_more: false,
      },
    },
  });
  expect(html).toContain("Next review deadline: 2026-10-30 12:00:00 UTC");
  expect(html).toContain("other claim revision");
  expect(html).toContain("deterministic-verifier");
  expect(html).not.toContain("Evidence observed");
});

it("renders the actual private and public route shells during SSR", async () => {
  const PrivateRoute = (
    await import("../../../routes/private/notes/+page.svelte")
  ).default;
  const PublicRoute = (
    await import("../../../routes/public/app/notes/+page.svelte")
  ).default;
  const privateResult = await render(PrivateRoute, {
    props: { data: { graph: { nodes: [], edges: [] } } },
  });
  const publicResult = await render(PublicRoute);
  expect(privateResult.html).toContain("notes-root");
  expect(publicResult.html).toContain("Public notes");
  expect(publicResult.html).not.toContain("Review history");
});
