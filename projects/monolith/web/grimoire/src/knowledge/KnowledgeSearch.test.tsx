import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { FactHit } from "../api/knowledge";
import { findToast } from "../test/app";
import type { Routes } from "../test/backend";
import { ids } from "../test/fixtures";
import { entityIds, searchHits } from "../test/fixtures/knowledge";
import { sessionId } from "../test/fixtures/session";
import { KnowledgeSearch } from "./KnowledgeSearch";
import { mount } from "./mount.testing";

const SEARCH = "GET /campaigns/{campaign_id}/knowledge/search";
const DISPUTE = "POST /campaigns/{campaign_id}/facts/{fact_id}/dispute";
const INPUT = "Search what your character knows";

const ownFact = searchHits.find((hit) => hit.type === "fact") as FactHit;
const partyFact: FactHit = {
  ...ownFact,
  id: "92222222-2222-4222-8222-222222222222",
  preview: "The party owes the ferryman two silver.",
  ownership: "party",
  can_dispute: false,
};

function render(routes: Routes) {
  const onOpenKnowledge = vi.fn();
  const onShowEvent = vi.fn();
  const mounted = mount(
    <KnowledgeSearch
      campaignId={ids.owned}
      onOpenKnowledge={onOpenKnowledge}
      onShowEvent={onShowEvent}
    />,
    routes,
  );
  const search = async (q: string) => {
    const input = panel().getByLabelText(INPUT);
    await mounted.user.clear(input);
    if (q) await mounted.user.type(input, q);
    await mounted.user.click(
      panel().getByRole("button", { name: "Search knowledge" }),
    );
  };
  return { ...mounted, search, onOpenKnowledge, onShowEvent };
}

// The result count is also sent to a live region, so text is read from the panel.
const panel = () =>
  within(screen.getByRole("region", { name: "Character knowledge search" }));
const results = () => panel().findByRole("list", { name: "Knowledge results" });

describe("character knowledge search", () => {
  it("sends the trimmed query and nothing before a search is asked for", async () => {
    const { backend, search } = render({ [SEARCH]: [] });
    expect(backend.calls).toHaveLength(0);
    await search(" clues & +/雪? ");
    await panel().findByText("No knowledge matches your search.");
    expect(backend.callsTo(SEARCH)).toHaveLength(1);
    expect(backend.callsTo(SEARCH)[0]?.query.get("q")).toBe("clues & +/雪?");
  });

  it("searches on Enter but not while an IME is composing", async () => {
    const { backend, user } = render({ [SEARCH]: [] });
    const input = panel().getByLabelText(INPUT);
    await user.type(input, "ゆき");
    fireEvent.keyDown(input, { key: "Enter", isComposing: true, keyCode: 229 });
    expect(backend.calls).toHaveLength(0);
    await user.keyboard("{Enter}");
    await waitFor(() => expect(backend.callsTo(SEARCH)).toHaveLength(1));
  });

  it("labels every kind of hit and links it to its source", async () => {
    const { search, user, onOpenKnowledge, onShowEvent } = render({
      [SEARCH]: searchHits,
    });
    await search("eel");
    const list = await results();
    const rows = within(list).getAllByRole("listitem");
    expect(
      rows.map(
        (row) => /^(Entity|Note|Event|Lore|Fact)/.exec(row.textContent)![0],
      ),
    ).toEqual(["Entity", "Note", "Event", "Lore", "Fact"]);
    expect(panel().getByText("5 results found.")).toBeTruthy();

    // An entity hit shows its revealed details only, and opens the drawer.
    expect(
      within(rows[0]!).getByText(
        "disposition: wary · clue: Counts the silver twice.",
      ),
    ).toBeTruthy();
    await user.click(
      within(rows[0]!).getByRole("button", { name: "Explore Orla Fenn" }),
    );
    expect(onOpenKnowledge).toHaveBeenCalledWith(entityIds.keeper);

    const note = searchHits[1]!.id;
    expect(
      within(rows[1]!)
        .getByRole("link", { name: "Cellar door" })
        .getAttribute("href"),
    ).toBe(`/campaigns/${ids.owned}/notes?note=${note}#note-${note}`);

    const event = within(rows[2]!).getByRole("link", {
      name: "Session event 1",
    });
    expect(event.getAttribute("href")).toBe(
      `/campaigns/${ids.owned}/session?session=${sessionId}`,
    );
    await user.click(event);
    expect(onShowEvent).toHaveBeenCalledWith(searchHits[2]!.id);

    expect(
      within(rows[3]!)
        .getByRole("link", { name: /Example Gazetteer/ })
        .getAttribute("href"),
    ).toBe(
      "https://jomcgi.dev/app/grimoire/book/example-gazetteer/c/gazetteer-0042",
    );
    for (const hit of searchHits.slice(1))
      expect(within(list).getByText(hit.preview!)).toBeTruthy();
  });

  it("falls back to an entity's type when no details were revealed", async () => {
    const { search } = render({
      [SEARCH]: [{ ...searchHits[0]!, revealed_details: undefined }],
    });
    await search("orla");
    expect(within(await results()).getByText("npc")).toBeTruthy();
    expect(panel().getByText("1 result found.")).toBeTruthy();
  });

  it("links no fact to lore and lets only an own fact be disputed", async () => {
    const { search, user, backend } = render({
      [SEARCH]: [ownFact, partyFact],
      [DISPUTE]: undefined,
    });
    await search("favour");
    const list = await results();
    expect(within(list).getByText(ownFact.preview!)).toBeTruthy();
    expect(within(list).getByText(partyFact.preview!)).toBeTruthy();
    expect(within(list).getByText("Character fact · Active")).toBeTruthy();
    expect(within(list).getByText("Party fact · Active")).toBeTruthy();
    expect(within(list).queryAllByRole("link")).toHaveLength(0);
    const buttons = within(list).getAllByRole("button");
    expect(buttons).toHaveLength(1);

    await user.click(buttons[0]!);
    expect(
      await within(list).findByText(
        "Character fact · Disputed. Excluded from campaign context.",
      ),
    ).toBeTruthy();
    expect(backend.callsTo(DISPUTE)[0]?.params.fact_id).toBe(ownFact.id);
    expect(within(list).queryAllByRole("button")).toHaveLength(0);
  });

  it("keeps a failed dispute retryable and hides the backend's reason", async () => {
    const { search, user } = render({
      [SEARCH]: [ownFact],
      [DISPUTE]: { status: 404, detail: "OTHER-PC-SECRET" },
    });
    await search("favour");
    const dispute = within(await results()).getByRole("button", {
      name: "Dispute fact",
    });
    await user.click(dispute);
    expect(
      await findToast("The fact could not be disputed. Try again."),
    ).toBeTruthy();
    expect(document.body.textContent).not.toContain("OTHER-PC-SECRET");
    expect(
      screen.getByRole<HTMLButtonElement>("button", { name: "Dispute fact" })
        .disabled,
    ).toBe(false);
  });

  it.each([403, 404, 503])(
    "shows a refusal with status %s without claiming an empty result",
    async (status) => {
      const { search } = render({
        [SEARCH]: { status, detail: "Search unavailable" },
      });
      await search("eel");
      expect(await panel().findByText("Search unavailable")).toBeTruthy();
      expect(
        panel().queryByText("No knowledge matches your search."),
      ).toBeNull();
      expect(panel().queryByText(/results? found/)).toBeNull();
    },
  );

  it("clears earlier private results when a later search fails", async () => {
    const { search, backend } = render({ [SEARCH]: searchHits });
    await search("eel");
    await results();
    backend.set({ [SEARCH]: { status: 503, detail: "Search unavailable" } });
    await search("another");
    expect(await panel().findByText("Search unavailable")).toBeTruthy();
    expect(
      panel().queryByRole("list", { name: "Knowledge results" }),
    ).toBeNull();
    expect(document.body.textContent).not.toContain(searchHits[1]!.preview);
  });

  it.each(["", "   ", "x".repeat(201)])(
    "rejects an invalid query without asking the backend",
    async (q) => {
      const { search, backend } = render({ [SEARCH]: searchHits });
      await search("eel");
      await results();
      const before = backend.calls.length;
      await search(q);
      expect(
        await panel().findByText("Enter a search of 1 to 200 characters."),
      ).toBeTruthy();
      expect(backend.calls).toHaveLength(before);
      expect(
        panel().queryByRole("list", { name: "Knowledge results" }),
      ).toBeNull();
    },
  );

  it("fails closed on a malformed response", async () => {
    const { search } = render({
      [SEARCH]: { results: [] } as never,
    });
    await search("eel");
    expect(
      await panel().findByText("Knowledge search is unavailable."),
    ).toBeTruthy();
    expect(panel().queryByText("No knowledge matches your search.")).toBeNull();
  });
});
