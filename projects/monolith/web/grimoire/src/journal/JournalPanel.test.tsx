import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Journal } from "../api/schema";
import { createQueryClient, Providers } from "../app/Providers";
import { journalIds, sessionJournal } from "../test/fixtures/journal";
import { JournalPanel, type JournalPanelProps } from "./JournalPanel";

function panel(props: Partial<JournalPanelProps> = {}) {
  const { container } = render(
    <MemoryRouter>
      <Providers client={createQueryClient()}>
        <JournalPanel journal={{}} view="mine" {...props} />
      </Providers>
    </MemoryRouter>,
  );
  return container;
}

const section = (name: string) => screen.getByRole("region", { name });
const buttonNames = () =>
  screen.getAllByRole("button").map((button) => button.textContent);

afterEach(() => vi.restoreAllMocks());

describe("JournalPanel", () => {
  it("renders every section using only the supplied projection", () => {
    const fetch = vi.spyOn(globalThis, "fetch");
    const root = panel({ journal: sessionJournal });
    expect(
      screen
        .getAllByRole("heading", { level: 2 })
        .map((heading) => heading.textContent),
    ).toEqual([
      "Learned",
      "Received",
      "People and places",
      "Rolls",
      "Open threads",
    ]);
    const people = section("People and places");
    expect(
      within(people)
        .getAllByRole("listitem")
        .map((item) => item.textContent),
    ).toEqual(["Old Tamnpc", "The Gilded Eellocation"]);
    expect(fetch).not.toHaveBeenCalled();
    expect(root.querySelectorAll("a")).toHaveLength(0);
  });

  it("never prints scopes, raw JSON, booleans or internal keys", () => {
    const text = panel({ journal: sessionJournal }).textContent ?? "";
    for (const dump of [
      "revealed details",
      "revealed_details",
      "{",
      "Scope",
      "name_only",
      "partial",
      "recognition only",
      "true",
      "false",
      "homebrew",
      "is global",
      "created at",
      "visibility",
      "modifier",
      "formula",
      "label",
      "total",
      "text",
      journalIds.ferryman,
      journalIds.cellar,
    ])
      expect(text).not.toContain(dump);
  });

  it("shows a partial reveal as its labelled details and a full one as its fields", () => {
    panel({ journal: sessionJournal });
    const [partial, , , full] = within(section("Learned"))
      .getAllByRole("listitem")
      .map((item) => within(item));
    expect(partial!.getByRole("heading", { name: "Old Tam" })).toBeTruthy();
    expect(partial!.getByText("npc")).toBeTruthy();
    expect(partial!.getByText("occupation")).toBeTruthy();
    expect(partial!.getByText("Ferryman")).toBeTruthy();
    for (const [label, value] of [
      ["location type", "Tavern"],
      ["region", "Harbour ward"],
      ["description", "A low room that smells of tar and eels."],
    ]) {
      expect(full!.getByText(label!)).toBeTruthy();
      expect(full!.getByText(value!)).toBeTruthy();
    }
  });

  it("shows a roll as its total, what was rolled, and the dice", () => {
    panel({ journal: sessionJournal });
    const rolls = within(section("Rolls")).getAllByRole("listitem");
    expect(rolls).toHaveLength(1);
    const roll = within(rolls[0]!);
    expect(roll.getByText("17")).toBeTruthy();
    expect(roll.getByText("Perception · 2d20kh1+3")).toBeTruthy();
    expect(roll.getByText("Dice: 6, 14 · Kept: 14")).toBeTruthy();
  });

  it("names an unlabelled roll Roll and leaves out dice it was not sent", () => {
    panel({
      journal: {
        rolls: [
          {
            id: journalIds.roll,
            kind: "roll",
            body: { formula: "1d6", total: 4, rolls: [4], kept: [4] },
          },
          { id: journalIds.action, kind: "roll", body: { total: 9 } },
        ],
      },
    });
    const [plain, bare] = within(section("Rolls")).getAllByRole("listitem");
    expect(within(plain!).getByText("Roll · 1d6")).toBeTruthy();
    // Kept repeats the dice when none were dropped.
    expect(within(plain!).getByText("Dice: 4")).toBeTruthy();
    expect(bare!.textContent).toBe("9Roll");
  });

  it("shows an open thread as the action's own words", () => {
    panel({ journal: sessionJournal });
    expect(
      within(section("Open threads"))
        .getAllByRole("listitem")
        .map((item) => item.textContent),
    ).toEqual(["I check the cellar door for a draught."]);
  });

  it("renders a received handout as its title and inert markdown", () => {
    panel({
      journal: {
        received: [
          {
            id: journalIds.handout,
            kind: "handout",
            body: {
              title: "Letter from the baron",
              markdown: "Meet me at **dusk**.<script>x()</script>",
            },
          },
        ],
      },
    });
    const received = section("Received");
    expect(
      within(received).getByRole("heading", { name: "Letter from the baron" }),
    ).toBeTruthy();
    expect(received.querySelector("strong")?.textContent).toBe("dusk");
    expect(received.querySelector("script")).toBeNull();
    // The handout is prose, not a field list of its body.
    expect(received.textContent).not.toContain("markdown");
  });

  it("marks a retracted Learned entry and never renders its details or scope", () => {
    const root = panel({
      journal: {
        learned: [
          {
            entity_id: journalIds.smuggler,
            name: "Known person",
            entity_type: "npc",
            retracted: true,
            grant_scope: "full",
            entity: { details: "must-never-survive" },
          },
        ],
      },
      onOpenKnowledge: vi.fn(),
    });
    expect(screen.getByRole("heading", { name: "Known person" })).toBeTruthy();
    expect(screen.getByText("Retracted")).toBeTruthy();
    expect(within(section("Learned")).getByRole("listitem").textContent).toBe(
      "Known personnpcRetracted",
    );
    expect(root.textContent).not.toContain("must-never-survive");
    expect(screen.queryByRole("button", { name: /Explore/ })).toBeNull();
  });

  it("shows a name-only entry as its name and recognition, and nothing else", () => {
    panel({
      journal: {
        learned: [
          {
            ...sessionJournal.learned![2]!,
            // Nothing beyond the name is shown, whatever the snapshot holds.
            entity: { recognition_only: true, occupation: "must-never-show" },
          },
        ],
      },
      showViewToggle: false,
      onOpenKnowledge: vi.fn(),
    });
    const learned = section("Learned");
    expect(
      within(learned).getByRole("heading", { name: "Sister Orla" }),
    ).toBeTruthy();
    expect(within(learned).getByRole("listitem").textContent).toBe(
      "Sister OrlaYou recognize this name.",
    );
    expect(screen.queryByRole("button", { name: /Explore/ })).toBeNull();
  });

  it("treats a snapshot marked recognition-only as name-only without a scope", () => {
    panel({
      journal: {
        learned: [
          {
            entity_id: journalIds.stranger,
            name: "Sister Orla",
            entity: { recognition_only: true },
          },
        ],
      },
      onOpenKnowledge: vi.fn(),
    });
    expect(screen.getAllByText("You recognize this name.")).toHaveLength(1);
    expect(screen.queryByRole("button", { name: /Explore/ })).toBeNull();
  });

  it("tells the DM which character learned an entry, and never prints an id", () => {
    const pc = "c3c3c3c3-c3c3-4c3c-8c3c-c3c3c3c3c3c3";
    const journal: Journal = {
      learned: [{ ...sessionJournal.learned![0]!, player_character_id: pc }],
    };
    const known = panel({ journal, characterNames: { [pc]: "Aria Fenn" } });
    expect(screen.getByText("Shown to Aria Fenn")).toBeTruthy();
    expect(known.textContent).not.toContain(pc);
    const unknown = panel({ journal });
    expect(unknown.textContent).not.toContain("Shown to");
    expect(unknown.textContent).not.toContain(pc);
  });

  it("says once that nothing is recorded, with no empty sections", () => {
    const root = panel({ journal: { learned: [] }, showViewToggle: false });
    expect(root.textContent).toBe("Nothing recorded yet.");
    expect(screen.queryAllByRole("heading")).toHaveLength(0);
    expect(root.querySelectorAll("li")).toHaveLength(0);
  });

  it("shows only the sections that have entries", () => {
    const root = panel({
      journal: { rolls: sessionJournal.rolls, open_threads: [] },
    });
    expect(
      screen.getAllByRole("heading").map((heading) => heading.textContent),
    ).toEqual(["Rolls"]);
    expect(root.textContent).not.toContain("Nothing recorded yet.");
    expect(root.textContent).not.toMatch(/No .* yet/);
  });

  it("shows a loading placeholder, not empty sections, while there is no journal", () => {
    const root = panel({ journal: null });
    expect(root.querySelector('[aria-busy="true"]')).not.toBeNull();
    expect(root.textContent).not.toContain("Nothing recorded yet.");
  });

  it("warns that a truncated journal is incomplete", () => {
    const root = panel({
      journal: { truncated: true, rolls: sessionJournal.rolls },
    });
    expect(screen.getByText("This journal is incomplete")).toBeTruthy();
    expect(root.textContent).toContain("Open threads may already");
    expect(section("Rolls")).toBeTruthy();
  });

  it("keeps the warning above the single empty line", () => {
    panel({ journal: { truncated: true } });
    expect(screen.getByText("This journal is incomplete")).toBeTruthy();
    expect(screen.getByText("Nothing recorded yet.")).toBeTruthy();
  });

  it("shows no partial warning for a complete journal", () => {
    panel({ journal: { truncated: false } });
    expect(screen.queryByText("This journal is incomplete")).toBeNull();
  });

  it.each(["mine", "party"] as const)(
    "renders the controlled %s view and forwards toggle requests",
    async (view) => {
      const onViewChange = vi.fn();
      panel({ view, onViewChange });
      const group = screen.getByRole("radiogroup", {
        name: "Journal audience",
      });
      const chosen = () =>
        within(group)
          .getAllByRole("radio")
          .filter((radio) => radio.getAttribute("aria-checked") === "true")
          .map((radio) => radio.textContent);
      expect(chosen()).toEqual([view === "mine" ? "Mine" : "Party"]);
      const other = view === "mine" ? "Party" : "Mine";
      await userEvent.click(within(group).getByRole("radio", { name: other }));
      expect(onViewChange).toHaveBeenLastCalledWith(other.toLowerCase());
      // The caller owns the selected view; the panel does not switch itself.
      expect(chosen()).toEqual([view === "mine" ? "Mine" : "Party"]);
    },
  );

  it("hides the audience toggle when the page owns the view", () => {
    panel({ showViewToggle: false, onViewChange: vi.fn() });
    expect(screen.queryByRole("radiogroup")).toBeNull();
  });

  it("renders all supplied text as text, including snapshot and event bodies", () => {
    const canary = '<img src=x onerror="alert(1)">';
    const root = panel({
      journal: {
        learned: [
          {
            entity_id: journalIds.ferryman,
            name: canary,
            entity: { details: canary, nested: { note: canary } },
            retracted: false,
          },
        ],
        received: [
          { id: journalIds.handout, body: { title: canary, markdown: canary } },
        ],
        rolls: [{ id: journalIds.roll, body: { label: canary, total: 3 } }],
        open_threads: [{ id: journalIds.action, body: { text: canary } }],
        people_and_places: [{ id: journalIds.cellar, name: canary }],
      },
    });
    expect(root.textContent).toContain(canary);
    expect(root.querySelector("img")).toBeNull();
  });

  describe("source navigation", () => {
    it("renders no navigation controls when no callback is given", () => {
      panel({ journal: sessionJournal, showViewToggle: false });
      expect(screen.queryAllByRole("button")).toHaveLength(0);
    });

    it("forwards each source event and only explorable knowledge", async () => {
      const onShowEvent = vi.fn();
      const onOpenKnowledge = vi.fn();
      panel({
        journal: sessionJournal,
        showViewToggle: false,
        onShowEvent,
        onOpenKnowledge,
      });
      expect(buttonNames()).toEqual([
        "Explore Old Tam",
        "Show in story",
        "Show in story",
        "Show in story",
        "Explore The Gilded Eel",
        "Show in story",
        "Show in story",
        "Show in story",
        "Show in story",
      ]);
      expect(
        screen.getByRole("button", {
          name: "Show I check the cellar door for a draught. in story",
        }),
      ).toBeTruthy();
      // Each "Show in story" names what it shows.
      expect(
        screen.getByRole("button", {
          name: "Show Letter from the harbourmaster in story",
        }),
      ).toBeTruthy();
      for (const button of screen.getAllByRole("button"))
        await userEvent.click(button);
      expect(onShowEvent.mock.calls.map(([id]) => id)).toEqual([
        journalIds.reveal,
        journalIds.retractedReveal,
        journalIds.recognition,
        journalIds.fullReveal,
        journalIds.handout,
        journalIds.roll,
        journalIds.action,
      ]);
      expect(onOpenKnowledge.mock.calls).toEqual([
        [journalIds.ferryman],
        [journalIds.cellar],
      ]);
    });

    it("offers story links without knowledge links when only onShowEvent is given", () => {
      panel({
        journal: sessionJournal,
        showViewToggle: false,
        onShowEvent: vi.fn(),
      });
      const names = buttonNames();
      expect(names).toHaveLength(7);
      expect(names.every((name) => name === "Show in story")).toBe(true);
    });
  });
});
