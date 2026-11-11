import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { MemberView, SessionEventView } from "../api/schema";
import type { TurnOrder } from "../api/session";
import type { TableCharacter } from "../screens/session/table";
import { chooseOption, findToast } from "../test/app";
import type { Routes } from "../test/backend";
import { ids, members } from "../test/fixtures";
import { event, sessionId } from "../test/fixtures/session";
import { dmOrder, turnsRoutes } from "../test/fixtures/turns";
import { InitiativeEditor } from "./InitiativeEditor";
import { renderPanel } from "./renderPanel";

const SET = "PUT /campaigns/{campaign_id}/sessions/{session_id}/initiative";
const ADVANCE =
  "POST /campaigns/{campaign_id}/sessions/{session_id}/initiative/advance";
const END = "DELETE /campaigns/{campaign_id}/sessions/{session_id}/initiative";

const mira = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
const characters: TableCharacter[] = [
  { id: ids.spare, name: "Brannoch Stone" },
  { id: mira, name: "Mira Vale" },
].map(({ id, name }) => ({
  id,
  campaign_id: ids.owned,
  player_name: null,
  character_name: name,
  class_name: "Fighter",
  level: 3,
  sheet: {},
  approved: null,
}));

function open(
  props: {
    initiative?: TurnOrder;
    events?: SessionEventView[];
    members?: MemberView[];
  } = {},
  routes: Routes = turnsRoutes,
) {
  const panel = (initiative: TurnOrder) => (
    <InitiativeEditor
      campaignId={ids.owned}
      sessionId={sessionId}
      initiative={initiative}
      characters={characters}
      events={props.events ?? []}
      members={props.members ?? []}
    />
  );
  const view = renderPanel(
    panel(props.initiative === undefined ? dmOrder : props.initiative),
    routes,
  );
  return {
    ...view,
    receive: (initiative: TurnOrder) => view.rerender(panel(initiative)),
  };
}

const rows = () => screen.queryAllByRole("listitem");
const names = () =>
  rows().map(
    (row) =>
      (within(row).getByRole("textbox", { name: "Name" }) as HTMLInputElement)
        .value,
  );
const row = (name: string) => screen.getByRole("listitem", { name });
const field = (name: string, label: string) =>
  within(row(name)).getByRole("textbox", { name: label }) as HTMLInputElement;
const button = (name: string) =>
  screen.getByRole("button", { name }) as HTMLButtonElement;
const held = (name: string) =>
  button(name).disabled ||
  button(name).getAttribute("aria-disabled") === "true";
const sent = (backend: { callsTo: (op: never) => { body: unknown }[] }) =>
  backend.callsTo(SET as never).at(-1)!.body as {
    entries: Array<Record<string, unknown>>;
    hidden_display: string;
    active_index: number;
    round: number;
  };

describe("InitiativeEditor", () => {
  it("lists the saved order and sends nothing until asked", () => {
    const { backend } = open();
    expect(names()).toEqual([
      "Brannoch Stone",
      "Cellar rats",
      "The Pale Warden",
    ]);
    expect(field("Cellar rats", "Initiative").value).toBe("12");
    expect(within(row("Brannoch Stone")).getByText("Active turn")).toBeTruthy();
    expect(backend.calls).toHaveLength(0);
  });

  it("saves the order unchanged with the active entry and round", async () => {
    const { user, backend } = open();
    await user.click(button("Save order"));
    await findToast("Initiative order saved.");
    expect(sent(backend)).toEqual({
      entries: dmOrder.entries,
      hidden_display: "mask",
      active_index: 0,
      round: 2,
    });
    expect(backend.callsTo(SET)[0]!.params).toEqual({
      campaign_id: ids.owned,
      session_id: sessionId,
    });
  });

  it("reorders with up and down and keeps the turn on the same entry", async () => {
    const { user, backend } = open();
    await user.click(button("Move Brannoch Stone down"));
    expect(names()).toEqual([
      "Cellar rats",
      "Brannoch Stone",
      "The Pale Warden",
    ]);
    await user.click(button("Move The Pale Warden up"));
    expect(names()).toEqual([
      "Cellar rats",
      "The Pale Warden",
      "Brannoch Stone",
    ]);
    await user.click(button("Save order"));
    await findToast("Initiative order saved.");
    expect(sent(backend).entries.map((entry) => entry.label)).toEqual([
      "Cellar rats",
      "The Pale Warden",
      "Brannoch Stone",
    ]);
    // Brannoch held the turn and is now third.
    expect(sent(backend).active_index).toBe(2);
    expect(sent(backend).round).toBe(2);
  });

  it("cannot move the first entry up or the last entry down", () => {
    open();
    expect(held("Move Brannoch Stone up")).toBe(true);
    expect(held("Move The Pale Warden down")).toBe(true);
    expect(held("Move Brannoch Stone down")).toBe(false);
  });

  it("removes an entry", async () => {
    const { user, backend } = open();
    await user.click(button("Remove Cellar rats"));
    expect(names()).toEqual(["Brannoch Stone", "The Pale Warden"]);
    await user.click(button("Save order"));
    await findToast("Initiative order saved.");
    expect(sent(backend).entries).toHaveLength(2);
  });

  it("hides an NPC and omits hidden entries from players when chosen", async () => {
    const { user, backend } = open();
    await user.click(
      within(row("Cellar rats")).getByRole("checkbox", { name: "Hidden" }),
    );
    await user.click(screen.getByRole("radio", { name: "Omit entirely" }));
    await user.click(button("Save order"));
    await findToast("Initiative order saved.");
    expect(sent(backend).hidden_display).toBe("omit");
    expect(sent(backend).entries.map((entry) => entry.hidden)).toEqual([
      false,
      true,
      true,
    ]);
  });

  it("does not allow hiding a player character", async () => {
    const { user, backend } = open();
    const hidden = (name: string) =>
      within(row(name)).getByRole("checkbox", {
        name: "Hidden",
      }) as HTMLInputElement;
    const locked = (name: string) =>
      hidden(name).disabled ||
      hidden(name).getAttribute("aria-disabled") === "true";
    expect(locked("Brannoch Stone")).toBe(true);
    expect(locked("Cellar rats")).toBe(false);
    await user.click(hidden("Brannoch Stone"));
    await user.click(button("Save order"));
    await findToast("Initiative order saved.");
    expect(sent(backend).entries[0]!.hidden).toBe(false);
  });

  it("clears the hidden flag when an NPC entry becomes a character", async () => {
    const { user, backend } = open({
      initiative: { ...dmOrder, entries: [dmOrder.entries![2]!] },
    });
    await chooseOption(row("The Pale Warden"), "Character", "Mira Vale");
    await user.click(button("Save order"));
    await findToast("Initiative order saved.");
    expect(sent(backend).entries).toEqual([
      {
        label: "The Pale Warden",
        player_character_id: mira,
        initiative: 9,
        hidden: false,
      },
    ]);
  });

  it("adds a row, named after the chosen character", async () => {
    const { user, backend } = open({ initiative: null });
    expect(screen.getByText("No one is in the order yet.")).toBeTruthy();
    await user.click(button("Add row"));
    // A blank name cannot be saved.
    expect(held("Save order")).toBe(true);
    await chooseOption(row("New entry"), "Character", "Mira Vale");
    expect(names()).toEqual(["Mira Vale"]);
    const initiative = field("Mira Vale", "Initiative");
    await user.clear(initiative);
    expect(held("Save order")).toBe(true);
    await user.type(initiative, "17");
    expect(held("Save order")).toBe(false);
    await user.click(button("Save order"));
    await findToast("Initiative order saved.");
    expect(sent(backend)).toEqual({
      entries: [
        {
          label: "Mira Vale",
          player_character_id: mira,
          initiative: 17,
          hidden: false,
        },
      ],
      hidden_display: "mask",
      active_index: 0,
      round: 1,
    });
  });

  it("refuses an initiative that is not a whole number", async () => {
    const { user } = open();
    const initiative = field("Cellar rats", "Initiative");
    await user.clear(initiative);
    await user.type(initiative, "7.5");
    expect(held("Save order")).toBe(true);
    expect(
      within(row("Cellar rats")).getByText("Use a whole number."),
    ).toBeTruthy();
  });

  it("trims the name and caps it at eighty characters", async () => {
    const { user, backend } = open();
    const name = field("Cellar rats", "Name");
    await user.clear(name);
    await user.type(name, `  ${"x".repeat(90)}`);
    await user.click(button("Save order"));
    await findToast("Initiative order saved.");
    expect(sent(backend).entries[1]!.label).toBe("x".repeat(78));
  });

  it("sorts by initiative, highest first, keeping ties in order", async () => {
    const npc = (label: string, initiative: number) => ({
      label,
      player_character_id: null,
      initiative,
      hidden: false,
    });
    const { user } = open({
      initiative: {
        ...dmOrder,
        entries: [
          npc("Low", 3),
          npc("TieA", 10),
          npc("High", 18),
          npc("TieB", 10),
        ],
      },
    });
    await user.click(button("Sort by initiative"));
    expect(names()).toEqual(["High", "TieA", "TieB", "Low"]);
  });

  it("fills character initiative from the latest unretracted initiative roll", async () => {
    const roll = (
      member: string,
      label: string,
      total: number,
      retracted = false,
    ) =>
      event({
        kind: "roll",
        author_member_id: member,
        retracted_at: retracted ? "2026-11-10T19:30:00Z" : null,
        body: { label, total, formula: "1d20" },
      });
    const seated = (member: MemberView, character: string): MemberView => ({
      ...member,
      player_character_id: character,
    });
    const { user } = open({
      initiative: {
        ...dmOrder,
        entries: [
          dmOrder.entries![0]!,
          {
            label: "Mira Vale",
            player_character_id: mira,
            initiative: 9,
            hidden: false,
          },
          dmOrder.entries![1]!,
        ],
      },
      members: [seated(members[0]!, ids.spare), seated(members[1]!, mira)],
      events: [
        roll(ids.dmMember, "Initiative", 4),
        roll(ids.dmMember, "initiative check", 19),
        roll(ids.dmMember, "Perception", 99),
        roll(ids.playerMember, "Initiative", 20, true),
        roll(ids.playerMember, "Initiative", 6),
        roll(ids.playerMember, "Initiative", 13, true),
      ],
    });
    await user.click(button("Use rolls"));
    await findToast("Filled 2 initiative values from rolls.");
    expect(
      rows().map(
        (item) =>
          (
            within(item).getByRole("textbox", {
              name: "Initiative",
            }) as HTMLInputElement
          ).value,
      ),
    ).toEqual(["19", "6", "12"]);
    expect(held("Next turn")).toBe(true);
  });

  it("says when no rolls match and leaves the order untouched", async () => {
    const { user } = open();
    await user.click(button("Use rolls"));
    await findToast("No initiative rolls found for these characters.");
    expect(field("Brannoch Stone", "Initiative").value).toBe("17");
    expect(held("Next turn")).toBe(false);
  });

  it("advances and steps back", async () => {
    const { user, backend } = open();
    await user.click(button("Next turn"));
    await waitFor(() => expect(backend.callsTo(ADVANCE)).toHaveLength(1));
    await waitFor(() => expect(held("Previous turn")).toBe(false));
    await user.click(button("Previous turn"));
    await waitFor(() => expect(backend.callsTo(ADVANCE)).toHaveLength(2));
    expect(backend.callsTo(ADVANCE).map((call) => call.body)).toEqual([
      { direction: "next" },
      { direction: "previous" },
    ]);
  });

  it("ends the encounter only after confirmation", async () => {
    const { user, backend } = open();
    await user.click(button("End encounter"));
    const dialog = await screen.findByRole("alertdialog");
    expect(
      within(dialog).getByText("The turn order will be cleared."),
    ).toBeTruthy();
    expect(backend.callsTo(END)).toHaveLength(0);
    await user.click(
      within(dialog).getByRole("button", { name: "End encounter" }),
    );
    await findToast("Encounter ended.");
    expect(backend.callsTo(END)).toHaveLength(1);
  });

  it("holds the turn controls until unsaved edits are saved", async () => {
    const { user } = open();
    expect(held("Next turn")).toBe(false);
    await user.click(button("Move Brannoch Stone down"));
    expect(held("Next turn")).toBe(true);
    expect(held("Previous turn")).toBe(true);
    expect(held("End encounter")).toBe(true);
    expect(
      screen.getByText("Save your changes to use the turn controls."),
    ).toBeTruthy();
    await user.click(button("Save order"));
    await waitFor(() => expect(held("Next turn")).toBe(false));
  });

  it("offers no turn controls before an order is saved", () => {
    open({ initiative: { round: 1, active_index: null, entries: [] } });
    expect(held("Next turn")).toBe(true);
    expect(held("End encounter")).toBe(true);
    expect(held("Sort by initiative")).toBe(true);
    expect(held("Use rolls")).toBe(true);
  });

  it("follows the server's order while there are no unsaved edits", () => {
    const { receive } = open();
    receive({ ...dmOrder, entries: dmOrder.entries!.slice(0, 2) });
    expect(names()).toEqual(["Brannoch Stone", "Cellar rats"]);
  });

  it("keeps unsaved edits when a poll brings a different order", async () => {
    const { user, receive } = open();
    const name = field("Cellar rats", "Name");
    await user.clear(name);
    await user.type(name, "Dire rats");
    receive({ ...dmOrder, entries: dmOrder.entries!.slice(0, 1) });
    expect(names()).toEqual(["Brannoch Stone", "Dire rats", "The Pale Warden"]);
  });

  it("keeps edits made while an earlier draft is still saving", async () => {
    let finish: () => void = () => {};
    const saving = new Promise<void>((resolve) => {
      finish = resolve;
    });
    const { user, backend } = open();
    // The reply is held back by replacing fetch's answer with a pending one.
    const original = globalThis.fetch;
    globalThis.fetch = async (...args) => {
      const response = await original(...args);
      await saving;
      return response;
    };
    await user.click(button("Move Brannoch Stone down"));
    await user.click(button("Save order"));
    await waitFor(() => expect(backend.callsTo(SET)).toHaveLength(1));
    await user.type(field("Cellar rats", "Name"), " again");
    finish();
    await findToast("Initiative order saved.");
    globalThis.fetch = original;
    expect(names()).toContain("Cellar rats again");
    // The newer edit is still unsaved, so the turn controls stay held.
    expect(held("Next turn")).toBe(true);
  });

  it("keeps the draft and reports a refused save", async () => {
    const { user } = open(
      {},
      {
        ...turnsRoutes,
        [SET]: { status: 409, detail: "initiative was changed concurrently" },
      },
    );
    await user.click(button("Remove Cellar rats"));
    await user.click(button("Save order"));
    await findToast("initiative was changed concurrently");
    expect(names()).toEqual(["Brannoch Stone", "The Pale Warden"]);
    expect(held("Next turn")).toBe(true);
  });
});
