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
  expect(html).not.toContain("success");
});
