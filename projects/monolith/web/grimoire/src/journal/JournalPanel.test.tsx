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
    const learned = section("Learned");
    expect(
      within(learned).getByRole("heading", { name: "Old Tam" }),
    ).toBeTruthy();
    expect(within(learned).getByText("Scope: partial")).toBeTruthy();
    expect(learned.textContent).toContain("Ferryman");
    const people = section("People and places");
    expect(
      within(people)
        .getAllByRole("listitem")
        .map((item) => item.textContent),
    ).toEqual(["Old Tamnpc", "The Gilded Eellocation"]);
    expect(section("Rolls").textContent).toContain("1d20+3");
    expect(section("Rolls").textContent).toContain("17");
    expect(section("Open threads").textContent).toContain(
      "I check the cellar door for a draught.",
    );
    expect(fetch).not.toHaveBeenCalled();
    expect(root.querySelectorAll("a")).toHaveLength(0);
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
    expect(root.textContent).not.toContain("must-never-survive");
    expect(root.textContent).not.toContain("Scope:");
    expect(screen.queryByRole("button", { name: /Explore/ })).toBeNull();
  });

  it("says a name is only recognised and offers nothing to explore", () => {
    panel({
      journal: { learned: [sessionJournal.learned![2]!] },
      onOpenKnowledge: vi.fn(),
    });
    expect(screen.getByText("You recognize this name.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Explore/ })).toBeNull();
  });

  it("tells the DM which character learned an entry, by name when known", () => {
    const pc = "c3c3c3c3-c3c3-4c3c-8c3c-c3c3c3c3c3c3";
    const journal: Journal = {
      learned: [{ ...sessionJournal.learned![0]!, player_character_id: pc }],
    };
    panel({ journal, characterNames: { [pc]: "Aria Fenn" } });
    expect(screen.getByText("Character: Aria Fenn")).toBeTruthy();
  });

  it("shows an empty state for every section, including missing ones", () => {
    const root = panel({ journal: { learned: [] } });
    for (const text of [
      "No discoveries yet.",
      "No handouts received yet.",
      "No people or places recorded yet.",
      "No rolls recorded yet.",
      "No open threads.",
    ])
      expect(screen.getByText(text)).toBeTruthy();
    expect(root.querySelectorAll("li")).toHaveLength(0);
  });

  it("shows a loading placeholder, not empty sections, while there is no journal", () => {
    const root = panel({ journal: null });
    expect(root.querySelector('[aria-busy="true"]')).not.toBeNull();
    expect(root.textContent).not.toContain("No discoveries yet.");
  });

  it("warns that a truncated journal is incomplete and qualifies empty states", () => {
    const root = panel({ journal: { truncated: true } });
    expect(screen.getByText("This journal is incomplete")).toBeTruthy();
    expect(root.textContent).toContain("Open threads may already");
    for (const text of [
      "No discoveries yet in the events shown.",
      "No handouts received yet in the events shown.",
      "No people or places recorded yet in the events shown.",
      "No rolls recorded yet in the events shown.",
      "No open threads in the events shown.",
    ])
      expect(screen.getByText(text)).toBeTruthy();
    expect(screen.queryByText("No open threads.")).toBeNull();
  });

  it("shows no partial warning for a complete journal", () => {
    const root = panel({ journal: { truncated: false } });
    expect(screen.queryByText("This journal is incomplete")).toBeNull();
    expect(root.textContent).not.toContain("in the events shown");
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
        received: [{ id: journalIds.handout, body: { text: canary } }],
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
        "Show in story",
        "Explore Old Tam",
        "Show in story",
        "Show in story",
        "Show in story",
        "Show in story",
        "Show in story",
      ]);
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
        journalIds.handout,
        journalIds.roll,
        journalIds.action,
      ]);
      expect(onOpenKnowledge.mock.calls).toEqual([[journalIds.ferryman]]);
    });

    it("offers story links without knowledge links when only onShowEvent is given", () => {
      panel({
        journal: sessionJournal,
        showViewToggle: false,
        onShowEvent: vi.fn(),
      });
      const names = buttonNames();
      expect(names).toHaveLength(6);
      expect(names.every((name) => name === "Show in story")).toBe(true);
    });
  });
});
