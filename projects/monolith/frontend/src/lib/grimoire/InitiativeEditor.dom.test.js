// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from "vitest";
import { mount, unmount } from "svelte";
import InitiativeEditor from "./InitiativeEditor.svelte";
import {
  buttonByText,
  chooseOption,
  setChecked,
  settle,
  typeInto,
} from "./test-helpers.js";

const pcA = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const pcB = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const characters = [
  { id: pcA, character_name: "Aria" },
  { id: pcB, character_name: "Bram" },
];
const initiative = {
  round: 2,
  active_index: 0,
  hidden_display: "mask",
  entries: [
    { label: "Aria", player_character_id: pcA, initiative: 12, hidden: false },
    { label: "Bram", player_character_id: pcB, initiative: 9, hidden: false },
    {
      label: "Ambusher",
      player_character_id: null,
      initiative: 15,
      hidden: false,
    },
  ],
};

let instance;
let act;
afterEach(async () => {
  if (instance) await unmount(instance);
  instance = undefined;
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
});

async function render(props = {}) {
  act = vi.fn(async () => ({ ok: true }));
  instance = mount(InitiativeEditor, {
    target: document.body,
    props: { initiative, characters, events: [], members: [], act, ...props },
  });
  await settle();
}
const rows = () => [...document.querySelectorAll(".row")];
const nameOf = (row) => row.querySelector("input").value;
const names = () => rows().map(nameOf);
const rowFor = (name) => rows().find((row) => nameOf(row) === name);
const click = async (text) => {
  buttonByText(document.body, text).click();
  await settle();
};
const ariaButton = (label) =>
  document.querySelector(`button[aria-label="${label}"]`);
const lastCall = () => act.mock.calls.at(-1)[0];

describe("initiative editor", () => {
  it("lists the saved order and sends nothing until asked", async () => {
    await render();
    expect(names()).toEqual(["Aria", "Bram", "Ambusher"]);
    expect(rowFor("Aria").querySelector("select").value).toBe(pcA);
    expect(act).not.toHaveBeenCalled();
  });

  it("saves the saved order unchanged with the active entry and round", async () => {
    await render();
    await click("Save order");
    expect(lastCall()).toEqual({
      operation: "initiativeSet",
      entries: initiative.entries,
      hiddenDisplay: "mask",
      activeIndex: 0,
      round: 2,
    });
  });

  it("reorders with up and down and keeps the turn on the same entry", async () => {
    await render();
    ariaButton("Move Aria down").click();
    await settle();
    expect(names()).toEqual(["Bram", "Aria", "Ambusher"]);
    ariaButton("Move Ambusher up").click();
    await settle();
    expect(names()).toEqual(["Bram", "Ambusher", "Aria"]);
    await click("Save order");
    expect(lastCall().entries.map((entry) => entry.label)).toEqual([
      "Bram",
      "Ambusher",
      "Aria",
    ]);
    // Aria held the turn and is now third.
    expect(lastCall().activeIndex).toBe(2);
    expect(lastCall().round).toBe(2);
  });

  it("cannot move the first entry up or the last entry down", async () => {
    await render();
    expect(ariaButton("Move Aria up").disabled).toBe(true);
    expect(ariaButton("Move Ambusher down").disabled).toBe(true);
    expect(ariaButton("Move Aria down").disabled).toBe(false);
  });

  it("removes an entry", async () => {
    await render();
    ariaButton("Remove Bram").click();
    await settle();
    expect(names()).toEqual(["Aria", "Ambusher"]);
    await click("Save order");
    expect(lastCall().entries).toHaveLength(2);
  });

  it("hides an NPC and omits it from players when chosen", async () => {
    await render();
    await setChecked(
      rowFor("Ambusher").querySelector('input[type="checkbox"]'),
    );
    await setChecked(
      document.querySelector('input[name="hidden-display"][value="omit"]'),
    );
    await click("Save order");
    expect(lastCall().hiddenDisplay).toBe("omit");
    expect(lastCall().entries.map((entry) => entry.hidden)).toEqual([
      false,
      false,
      true,
    ]);
  });

  it("does not allow hiding a player character row", async () => {
    await render();
    expect(
      rowFor("Aria").querySelector('input[type="checkbox"]').disabled,
    ).toBe(true);
    expect(
      rowFor("Ambusher").querySelector('input[type="checkbox"]').disabled,
    ).toBe(false);
  });

  it("clears the hidden flag when an NPC row becomes a character", async () => {
    await render({
      initiative: {
        ...initiative,
        entries: [
          {
            label: "Ambusher",
            player_character_id: null,
            initiative: 15,
            hidden: true,
          },
        ],
      },
    });
    await chooseOption(rowFor("Ambusher").querySelector("select"), pcB);
    await click("Save order");
    expect(lastCall().entries).toEqual([
      {
        label: "Ambusher",
        player_character_id: pcB,
        initiative: 15,
        hidden: false,
      },
    ]);
  });

  it("adds a row with a name, character and initiative", async () => {
    await render({ initiative: null });
    expect(names()).toEqual([]);
    await click("Add row");
    const row = rows()[0];
    // A blank name cannot be saved.
    expect(buttonByText(document.body, "Save order").disabled).toBe(true);
    await chooseOption(row.querySelector("select"), pcB);
    expect(nameOf(row)).toBe("Bram");
    await typeInto(row.querySelector('input[type="number"]'), "17");
    expect(buttonByText(document.body, "Save order").disabled).toBe(false);
    await click("Save order");
    expect(lastCall()).toEqual({
      operation: "initiativeSet",
      entries: [
        {
          label: "Bram",
          player_character_id: pcB,
          initiative: 17,
          hidden: false,
        },
      ],
      hiddenDisplay: "mask",
      activeIndex: 0,
      round: 1,
    });
  });

  it("sorts by initiative, highest first, keeping ties in order", async () => {
    await render({
      initiative: {
        ...initiative,
        entries: [
          {
            label: "Low",
            player_character_id: null,
            initiative: 3,
            hidden: false,
          },
          {
            label: "TieA",
            player_character_id: null,
            initiative: 10,
            hidden: false,
          },
          {
            label: "High",
            player_character_id: null,
            initiative: 18,
            hidden: false,
          },
          {
            label: "TieB",
            player_character_id: null,
            initiative: 10,
            hidden: false,
          },
        ],
      },
    });
    await click("Sort by initiative");
    expect(names()).toEqual(["High", "TieA", "TieB", "Low"]);
  });

  it("fills character initiative from the latest matching visible roll", async () => {
    const roll = (id, seq, member, label, total, extra = {}) => ({
      id,
      seq,
      kind: "roll",
      author_member_id: member,
      retracted_at: null,
      body: { label, total, formula: "d20" },
      ...extra,
    });
    await render({
      members: [
        { id: "m-a", player_character_id: pcA },
        { id: "m-b", player_character_id: pcB },
      ],
      events: [
        roll("r1", 1, "m-a", "Initiative", 4),
        roll("r2", 2, "m-a", "initiative check", 17),
        roll("r3", 3, "m-a", "Perception", 99),
        roll("r4", 4, "m-b", "Initiative", 20, {
          retracted_at: "2026-10-10T12:00:00Z",
        }),
        roll("r5", 5, "m-b", "Initiative", 6),
        roll("r6", 6, "m-b", "Initiative", 13, {
          retracted_at: "2026-10-10T12:00:00Z",
        }),
      ],
    });
    await click("Use rolls");
    const values = rows().map(
      (row) => row.querySelector('input[type="number"]').value,
    );
    // Aria: latest initiative roll. Bram: latest unretracted one. NPC untouched.
    expect(values).toEqual(["17", "6", "15"]);
    expect(document.body.textContent).toContain("Filled 2 initiative values");
  });

  it("reports when no rolls match and leaves the numbers alone", async () => {
    await render();
    await click("Use rolls");
    expect(
      rows().map((row) => row.querySelector('input[type="number"]').value),
    ).toEqual(["12", "9", "15"]);
    expect(document.body.textContent).toContain("No initiative rolls found");
  });

  it("advances and steps back through the BFF operations", async () => {
    await render();
    await click("Next turn");
    expect(act).toHaveBeenLastCalledWith({
      operation: "initiativeAdvance",
      direction: "next",
    });
    await click("Previous turn");
    expect(act).toHaveBeenLastCalledWith({
      operation: "initiativeAdvance",
      direction: "previous",
    });
    expect(act).toHaveBeenCalledTimes(2);
  });

  it("ends the encounter only after confirmation", async () => {
    await render();
    vi.stubGlobal(
      "confirm",
      vi.fn(() => false),
    );
    await click("End encounter");
    expect(act).not.toHaveBeenCalled();
    vi.stubGlobal(
      "confirm",
      vi.fn(() => true),
    );
    await click("End encounter");
    expect(act).toHaveBeenCalledOnce();
    expect(lastCall()).toEqual({ operation: "initiativeEnd" });
  });

  it("holds the turn controls until unsaved edits are saved", async () => {
    await render();
    expect(buttonByText(document.body, "Next turn").disabled).toBe(false);
    ariaButton("Move Aria down").click();
    await settle();
    expect(buttonByText(document.body, "Next turn").disabled).toBe(true);
    expect(buttonByText(document.body, "End encounter").disabled).toBe(true);
    await click("Save order");
    expect(buttonByText(document.body, "Next turn").disabled).toBe(false);
  });

  it("offers no turn controls before an order is saved", async () => {
    await render({ initiative: null });
    expect(buttonByText(document.body, "Next turn").disabled).toBe(true);
    expect(buttonByText(document.body, "End encounter").disabled).toBe(true);
  });

  it("edits a name in place without saving", async () => {
    await render();
    await typeInto(rowFor("Aria").querySelector("input"), "Aria the Bold");
    expect(names()[0]).toBe("Aria the Bold");
    expect(act).not.toHaveBeenCalled();
  });

  it("does not save while a request is in flight", async () => {
    await render({ busy: true });
    expect(buttonByText(document.body, "Save order").disabled).toBe(true);
    expect(buttonByText(document.body, "Next turn").disabled).toBe(true);
  });
});
