// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from "vitest";
import { mount, unmount } from "svelte";
import Page from "./+page.svelte";

let mounted;
afterEach(() => {
  if (mounted) unmount(mounted);
  document.body.innerHTML = "";
  mounted = null;
  vi.unstubAllGlobals();
});

describe("entity observation and review dates", () => {
  it.each([null, "2026-10-13T08:00:00+00:00"])(
    "keeps observation distinct from review %s",
    (lastReviewedAt) => {
      vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false }));
      const target = document.createElement("div");
      document.body.append(target);
      const entity = {
        kind: "project",
        slug: "monolith",
        title: "Monolith",
        note_counts: { verified: 1, unverified: 0 },
      };
      mounted = mount(Page, {
        target,
        props: {
          data: {
            entity: "monolith",
            q: "",
            entities: [entity],
            projects: [entity],
            facts: { totals: { verified: 1 }, contradictions: 0 },
            results: [],
            unavailable: {},
            chapter: {
              entity,
              contradictions: [],
              notes: [
                {
                  note_id: "fact",
                  title: "A reviewed fact",
                  verification_state: "verified",
                  observed_at: "2026-10-11T08:00:00+00:00",
                  last_reviewed_at: lastReviewedAt,
                  snippet: "Evidence",
                },
              ],
            },
          },
        },
      });
      expect(target.textContent).toContain("last observed 10·11");
      if (lastReviewedAt) {
        expect(target.textContent).toContain("last reviewed 10·13");
        expect(target.querySelector(".src").textContent).toContain(
          "reviewed 10·13",
        );
      } else {
        expect(target.textContent).not.toContain("last reviewed");
      }
    },
  );
});
