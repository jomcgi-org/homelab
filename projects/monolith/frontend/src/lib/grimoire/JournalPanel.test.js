// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import JournalPanel from "./JournalPanel.svelte";

const emptyJournal = () => ({
  learned: [],
  received: [],
  people_and_places: [],
  rolls: [],
  open_threads: [],
});
const mounted = [];

async function panel(props = {}) {
  const target = document.createElement("div");
  document.body.append(target);
  target.addEventListener("submit", (event) => event.preventDefault());
  const instance = mount(JournalPanel, {
    target,
    props: { journal: emptyJournal(), ...props },
  });
  mounted.push({ instance, target });
  await tick();
  return target;
}

afterEach(async () => {
  for (const { instance, target } of mounted.splice(0)) {
    await unmount(instance);
    target.remove();
  }
  vi.restoreAllMocks();
});

describe("JournalPanel", () => {
  it("renders every section using only the supplied projection", async () => {
    const fetch = vi.spyOn(globalThis, "fetch");
    const root = await panel({
      journal: {
        learned: [
          {
            entity_id: "npc",
            name: "Mira",
            entity_type: "npc",
            grant_scope: "partial",
            retracted: false,
            entity: { revealed_details: { occupation: "Guide" } },
          },
        ],
        received: [{ id: "handout", body: { text: "A map of the pass" } }],
        people_and_places: [
          { id: "place", name: "North pass", entity_type: "location" },
        ],
        rolls: [{ id: "roll", body: { expression: "1d20", total: 17 } }],
        open_threads: [{ id: "action", body: { text: "Where is the key?" } }],
      },
    });
    expect(
      [...root.querySelectorAll("h3")].map((heading) => heading.textContent),
    ).toEqual([
      "Learned",
      "Received",
      "People and places",
      "Rolls",
      "Open threads",
    ]);
    for (const text of [
      "Mira",
      "partial",
      "Guide",
      "A map of the pass",
      "North pass",
      "1d20",
      "17",
      "Where is the key?",
    ])
      expect(root.textContent).toContain(text);
    expect(fetch).not.toHaveBeenCalled();
    expect(root.querySelectorAll("a")).toHaveLength(0);
  });

  it("marks a retracted Learned entry and never renders its details or scope", async () => {
    const root = await panel({
      journal: {
        ...emptyJournal(),
        learned: [
          {
            entity_id: "npc",
            name: "Known person",
            entity_type: "npc",
            retracted: true,
            grant_scope: "full",
            entity: { details: "must-never-survive" },
          },
        ],
      },
    });
    expect(root.textContent).toContain("Known person");
    expect(root.querySelector(".retracted").textContent).toBe("Retracted");
    expect(root.textContent).not.toContain("must-never-survive");
    expect(root.textContent).not.toContain("Scope:");
    expect(root.querySelector("dl")).toBeNull();
  });

  it("shows an empty state for every section", async () => {
    const root = await panel();
    for (const text of [
      "No discoveries yet.",
      "No handouts received yet.",
      "No people or places recorded yet.",
      "No rolls recorded yet.",
      "No open threads.",
    ])
      expect(root.textContent).toContain(text);
    expect(root.querySelectorAll("article")).toHaveLength(0);
  });

  it.each(["mine", "party"])(
    "renders the controlled %s view and forwards toggle requests",
    async (view) => {
      const onViewChange = vi.fn();
      const root = await panel({ view, onViewChange });
      const buttons = [...root.querySelectorAll("button")];
      expect(
        buttons
          .find((button) => button.value === view)
          .getAttribute("aria-pressed"),
      ).toBe("true");
      buttons[1].click();
      await tick();
      expect(onViewChange).toHaveBeenLastCalledWith("party");
      buttons[0].click();
      await tick();
      expect(onViewChange).toHaveBeenLastCalledWith("mine");
      // The caller owns both the selected view and the freshly polled projection.
      expect(
        buttons
          .find((button) => button.value === view)
          .getAttribute("aria-pressed"),
      ).toBe("true");
    },
  );

  it("supports GET view selection without a callback and hiding the toggle", async () => {
    const root = await panel();
    const button = root.querySelector('button[value="party"]');
    expect(button.name).toBe("view");
    expect(button.type).toBe("submit");
    expect(root.querySelector("form").getAttribute("method")).toBe("GET");
    button.click();
    const embedded = await panel({ showViewToggle: false });
    expect(embedded.querySelector("form")).toBeNull();
  });

  it("escapes all supplied text, including snapshot and event bodies", async () => {
    const canary = '<img src=x onerror="alert(1)">';
    const root = await panel({
      journal: {
        ...emptyJournal(),
        learned: [
          {
            entity_id: "npc",
            name: canary,
            entity: { details: canary },
            retracted: false,
          },
        ],
        received: [{ id: "handout", body: { text: canary } }],
      },
    });
    expect(root.textContent).toContain(canary);
    expect(root.querySelector("img")).toBeNull();
  });
});
