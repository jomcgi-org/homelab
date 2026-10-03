import { describe, expect, it } from "vitest";
import { render } from "svelte/server";
import JournalPanel from "./SessionJournalPanel.svelte";

describe("journal view", () => {
  it("renders received data and excludes the unselected party view", () => {
    const { body } = render(JournalPanel, {
      props: {
        views: {
          mine: {
            learned: [
              {
                name: "Mara",
                event_id: "event",
                projection: { revealed_details: { clue: "Visible clue" } },
              },
            ],
            rolls: [{ total: 17, formula: "d20+2", label: "My roll" }],
          },
          party: { learned: [{ name: "PARTY_ONLY_CANARY" }] },
        },
        showEvent() {},
      },
    });
    expect(body).toContain("Visible clue");
    expect(body).toContain("My roll");
    expect(body).toContain("Party journal");
    expect(body).not.toContain("PARTY_ONLY_CANARY");
  });
  it("renders an explicit retraction without its previous clue", () => {
    const { body } = render(JournalPanel, {
      props: {
        views: {
          mine: {
            learned: [
              {
                name: "Mara",
                retracted: true,
                projection: { description: "OLD_CLUE" },
              },
            ],
          },
        },
        showEvent() {},
      },
    });
    expect(body).toContain("Knowledge retracted: Mara.");
    expect(body).not.toContain("OLD_CLUE");
  });
});
