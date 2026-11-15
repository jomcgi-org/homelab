import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { InventoryItem } from "../api/inventory";
import { createQueryClient, Providers } from "../app/Providers";
import type { TableCharacter } from "../screens/session/table";
import { chooseOption, findToast, openApp } from "../test/app";
import { installBackend, type Backend, type Routes } from "../test/backend";
import { characters, ids, lobby } from "../test/fixtures";
import {
  inventoryIds,
  inventoryStore,
  playerInventoryItems,
} from "../test/fixtures/inventory";
import { sessionRoutes } from "../test/fixtures/session";
import { InventoryPanel, type InventoryPanelProps } from "./InventoryPanel";

const brannoch: TableCharacter = { ...characters[0]!, approved: null };
const wren: TableCharacter = {
  ...brannoch,
  id: "1e000000-0000-4000-8000-000000000001",
  character_name: "Wren Ashdown",
};
const eventId = "1f000000-0000-4000-8000-000000000001";

const ITEMS = "GET /campaigns/{campaign_id}/inventory";
const CHANGES = "GET /campaigns/{campaign_id}/inventory/changes";
const GIVE = "POST /campaigns/{campaign_id}/inventory";
const PATCH = "PATCH /campaigns/{campaign_id}/inventory/{item_id}";
const MOVE = "POST /campaigns/{campaign_id}/inventory/{item_id}/move";
const DELETE = "DELETE /campaigns/{campaign_id}/inventory/{item_id}";

let backend: Backend | undefined;
afterEach(() => {
  backend?.restore();
  backend = undefined;
});

function Panel(props: Partial<InventoryPanelProps>) {
  return (
    <MemoryRouter>
      <Providers client={createQueryClient()}>
        <InventoryPanel
          campaignId={ids.owned}
          dm={false}
          characters={[brannoch]}
          prefill={null}
          onOpenKnowledge={() => {}}
          {...props}
        />
      </Providers>
    </MemoryRouter>
  );
}

/** Mounts the panel as a player holding the rope, unless told otherwise. */
async function openPanel(
  props: Partial<InventoryPanelProps> = {},
  routes: Routes = props.dm ? inventoryStore() : playerStore(),
) {
  const installed = installBackend(routes);
  backend = installed;
  const view = render(<Panel {...props} />);
  await screen.findByRole("heading", { name: "Party pool" });
  return { user: userEvent.setup(), backend: installed, ...view };
}

const playerStore = (items = playerInventoryItems) =>
  inventoryStore(items, { viewer: ids.spare });
const dmProps = { dm: true, characters: [brannoch, wren] };
const button = (name: string) => screen.getByRole("button", { name });
const noButton = (name: string) => screen.queryByRole("button", { name });
const dialog = () => screen.getByRole("dialog");

/** A number field commits its value when focus leaves it. */
async function setNumber(user: UserEvent, label: RegExp, value: string) {
  const field = within(dialog()).getByLabelText(label);
  await user.clear(field);
  await user.type(field, value);
  await user.tab();
}

describe("InventoryPanel", () => {
  it("limits a player to their quantity and pool moves, naming no character", async () => {
    await openPanel();
    expect(screen.getByRole("region", { name: "Party pool" })).toBeTruthy();
    const mine = screen.getByRole("region", { name: "Your inventory" });
    expect(
      within(mine).getByRole("heading", { name: "Silk rope × 5" }),
    ).toBeTruthy();
    expect(button("Take Potion of healing")).toBeTruthy();
    expect(button("Move Silk rope to party pool")).toBeTruthy();
    expect(button("Adjust quantity for Silk rope")).toBeTruthy();
    expect(noButton("Adjust quantity for Potion of healing")).toBeNull();
    for (const name of ["Give item", "Delete Silk rope", "Edit Silk rope"])
      expect(noButton(name)).toBeNull();
    expect(screen.queryByText(/Brannoch|Wren/)).toBeNull();
    expect(screen.queryByText("Smuggler's ledger")).toBeNull();
  });

  it("gives the DM a section per character and every control", async () => {
    const { user } = await openPanel(dmProps);
    const pack = screen.getByRole("region", { name: "Brannoch Stone" });
    expect(within(pack).getByText("Silk rope")).toBeTruthy();
    const empty = screen.getByRole("region", { name: "Wren Ashdown" });
    expect(within(empty).getByText("No items here yet.")).toBeTruthy();
    const pool = screen.getByRole("region", { name: "Party pool" });
    const ledger = within(pool)
      .getByRole("heading", { name: /Smuggler's ledger/ })
      .closest("li")!;
    expect(within(ledger).getByText("Hidden from party")).toBeTruthy();
    for (const name of ["Give item", "Move Silk rope", "History for Silk rope"])
      expect(button(name)).toBeTruthy();
    // Deleting is not a row action: it lives in the edit form.
    expect(noButton("Delete Silk rope")).toBeNull();
    await user.click(button("Edit Silk rope"));
    const form = within(dialog());
    expect(form.getByRole("button", { name: "Delete Silk rope" })).toBeTruthy();
    expect(form.getByLabelText(/^Name/)).toHaveProperty("value", "Silk rope");
    expect(form.getByLabelText("Hidden from party")).toBeTruthy();
  });

  it("keeps a hidden item's badge and quantity edit but offers a player no move", async () => {
    const hidden: InventoryItem[] = playerInventoryItems.map((row) =>
      row.id === inventoryIds.rope ? { ...row, hidden_from_party: true } : row,
    );
    await openPanel({}, playerStore(hidden));
    expect(screen.getByText("Hidden from party")).toBeTruthy();
    expect(button("Adjust quantity for Silk rope")).toBeTruthy();
    expect(noButton("Move Silk rope to party pool")).toBeNull();
  });

  it("offers a member without a character nothing to take", async () => {
    await openPanel({ characters: [] });
    expect(noButton("Take Potion of healing")).toBeNull();
  });

  it("sends a player's move, take and quantity changes and refreshes the list", async () => {
    const { user, backend } = await openPanel();
    await user.click(button("Move Silk rope to party pool"));
    await setNumber(user, /^Quantity/, "2");
    await user.type(within(dialog()).getByLabelText(/^Reason/), "Share");
    await user.click(
      within(dialog()).getByRole("button", {
        name: "Move Silk rope to party pool",
      }),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(
      await screen.findByRole("heading", { name: "Silk rope × 3" }),
    ).toBeTruthy();

    await user.click(button("Take Potion of healing"));
    await setNumber(user, /^Quantity/, "1");
    await user.click(
      within(dialog()).getByRole("button", { name: "Take Potion of healing" }),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    const mine = within(screen.getByRole("region", { name: "Your inventory" }));
    await user.click(
      mine.getByRole("button", { name: "Adjust quantity for Silk rope" }),
    );
    // A player's quantity form offers no delete.
    expect(noButton("Delete Silk rope")).toBeNull();
    await setNumber(user, /^Quantity/, "0");
    await user.type(within(dialog()).getByLabelText(/^Reason/), "Consumed");
    await user.click(within(dialog()).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    expect(backend.callsTo(MOVE).map((call) => call.body)).toEqual([
      { owner: "party", quantity: 2, reason: "Share" },
      { owner: ids.spare, quantity: 1, reason: "" },
    ]);
    expect(backend.callsTo(MOVE)[0]?.params.item_id).toBe(inventoryIds.rope);
    const [patch] = backend.callsTo(PATCH);
    expect(patch?.params.item_id).toBe(inventoryIds.rope);
    // A player's patch carries the quantity alone; the backend refuses more.
    expect(patch?.body).toEqual({ quantity: 0, reason: "Consumed" });
    expect(backend.callsTo(ITEMS).length).toBeGreaterThanOrEqual(4);
    expect(
      await mine.findByRole("heading", { name: "Silk rope × 0" }),
    ).toBeTruthy();
    expect(
      mine.getByRole("button", { name: "Move Silk rope to party pool" }),
    ).toHaveProperty("disabled", true);
  });

  it("opens the give form from a feed event and posts it with the event's links", async () => {
    const prefill = {
      owner: ids.spare,
      name: "Silver key",
      entity_id: inventoryIds.moonRope,
      source_event_id: eventId,
    };
    const onOpenKnowledge = vi.fn();
    const { user, backend, unmount } = await openPanel({
      ...dmProps,
      prefill,
      onOpenKnowledge,
    });
    const form = within(await screen.findByRole("dialog"));
    expect(form.getByLabelText(/^Item name/)).toHaveProperty(
      "value",
      "Silver key",
    );
    expect(
      form.getByRole("combobox", { name: "Give to" }).textContent,
    ).toContain("Brannoch Stone");
    await user.click(
      form.getByRole("button", { name: "Open knowledge for Silver key" }),
    );
    expect(onOpenKnowledge).toHaveBeenCalledWith(inventoryIds.moonRope);
    await setNumber(user, /^Quantity/, "2");
    await user.type(form.getByLabelText(/^Notes/), "Reward");
    await user.type(form.getByLabelText(/^Reason/), "Found in chest");
    await user.click(form.getByLabelText("Hidden from party"));
    await user.click(form.getByRole("button", { name: "Give Silver key" }));
    expect(await findToast("Gave Silver key.")).toBeTruthy();
    expect(backend.callsTo(GIVE).map((call) => call.body)).toEqual([
      {
        owner: ids.spare,
        name: "Silver key",
        quantity: 2,
        notes: "Reward",
        entity_id: inventoryIds.moonRope,
        hidden_from_party: true,
        reason: "Found in chest",
        source_event_id: eventId,
      },
    ]);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(await screen.findByText("Silver key")).toBeTruthy();

    // Returning to the tab remounts the panel with the same prefill.
    unmount();
    render(<Panel {...dmProps} prefill={prefill} />);
    await screen.findByRole("heading", { name: "Party pool" });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("gives a blank item to the party pool by default and requires a name", async () => {
    const { user, backend } = await openPanel(dmProps);
    await user.click(button("Give item"));
    const form = within(dialog());
    expect(form.getByRole("button", { name: "Give item" })).toHaveProperty(
      "disabled",
      true,
    );
    await user.type(form.getByLabelText(/^Item name/), "  Torch ");
    await chooseOption(dialog(), "Give to", "Wren Ashdown");
    await user.click(form.getByRole("button", { name: "Give Torch" }));
    await waitFor(() => expect(backend.callsTo(GIVE)).toHaveLength(1));
    expect(backend.callsTo(GIVE)[0]?.body).toEqual({
      owner: wren.id,
      name: "Torch",
      quantity: 1,
      notes: "",
      entity_id: null,
      hidden_from_party: false,
      reason: "",
      source_event_id: null,
    });
  });

  it("keeps what the DM typed when the give is refused", async () => {
    const { user, backend } = await openPanel(dmProps, {
      ...inventoryStore(),
      [GIVE]: { status: 400, detail: "Save failed" },
    });
    await user.click(button("Give item"));
    const form = within(dialog());
    await user.type(form.getByLabelText(/^Item name/), "Keep this name");
    await user.type(form.getByLabelText(/^Notes/), "Keep notes");
    await user.click(form.getByRole("button", { name: "Give Keep this name" }));
    expect(await findToast("Save failed")).toBeTruthy();
    expect(form.getByLabelText(/^Item name/)).toHaveProperty(
      "value",
      "Keep this name",
    );
    expect(form.getByLabelText(/^Notes/)).toHaveProperty("value", "Keep notes");
    expect(backend.callsTo(GIVE)).toHaveLength(1);
  });

  it("keeps a player's quantity and reason when the save is refused", async () => {
    const { user } = await openPanel(
      {},
      {
        ...playerStore(),
        [PATCH]: { status: 403, detail: "inventory edit not permitted" },
      },
    );
    await user.click(button("Adjust quantity for Silk rope"));
    await setNumber(user, /^Quantity/, "4");
    const form = within(dialog());
    await user.type(form.getByLabelText(/^Reason/), "Used a length");
    await user.click(form.getByRole("button", { name: "Save" }));
    expect(await findToast("inventory edit not permitted")).toBeTruthy();
    expect(form.getByLabelText(/^Quantity/)).toHaveProperty("value", "4");
    expect(form.getByLabelText(/^Reason/)).toHaveProperty(
      "value",
      "Used a length",
    );
  });

  it("will not move more than the item holds", async () => {
    const { user, backend } = await openPanel();
    await user.click(button("Take Potion of healing"));
    await setNumber(user, /^Quantity/, "9");
    const form = within(dialog());
    expect(form.getByLabelText(/^Quantity/)).toHaveProperty("value", "3");
    await user.click(
      form.getByRole("button", { name: "Take Potion of healing" }),
    );
    await waitFor(() => expect(backend.callsTo(MOVE)).toHaveLength(1));
    expect(backend.callsTo(MOVE)[0]?.body).toMatchObject({ quantity: 3 });
  });

  it("loads an item's history when opened and names the owners", async () => {
    const onOpenKnowledge = vi.fn();
    const { user, backend } = await openPanel({ ...dmProps, onOpenKnowledge });
    await user.click(button("Explore Moon rope for Silk rope"));
    expect(onOpenKnowledge).toHaveBeenCalledWith(inventoryIds.moonRope);
    expect(backend.callsTo(CHANGES)).toHaveLength(0);
    await user.click(button("History for Silk rope"));
    expect(
      await screen.findByText(/Owner: Brannoch Stone to party\./),
    ).toBeTruthy();
    expect(backend.callsTo(CHANGES)[0]?.query.get("item_id")).toBe(
      inventoryIds.rope,
    );
    const entry = screen.getByText("Shared with the party").closest("li")!;
    expect(entry.textContent).toContain("move: delta -2, quantity after 5.");
    expect(entry.querySelector("time")?.dateTime).toBe(
      "2026-11-10T19:20:00+00:00",
    );
  });

  it("leaves a player's projected owners as the backend sent them", async () => {
    const store = playerStore();
    const { user } = await openPanel(
      {},
      {
        ...store,
        [CHANGES]: [
          {
            id: "1c000000-0000-4000-8000-0000000000aa",
            item_id: inventoryIds.rope,
            action: "move",
            delta: 0,
            quantity_after: 5,
            reason: "",
            created_at: "2026-11-10T19:20:00+00:00",
            changes: { owner: { from: "you", to: "character" } },
          },
        ],
      },
    );
    await user.click(button("History for Silk rope"));
    expect(await screen.findByText(/Owner: you to character\./)).toBeTruthy();
  });

  it("edits and moves an item as the DM, and deletes only after confirming", async () => {
    const { user, backend } = await openPanel(dmProps);
    await user.click(button("Edit Silk rope"));
    let form = within(dialog());
    await user.clear(form.getByLabelText(/^Name/));
    await user.type(form.getByLabelText(/^Name/), "Long rope");
    await user.clear(form.getByLabelText(/^Notes/));
    await user.type(form.getByLabelText(/^Notes/), "Frayed");
    await user.click(form.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(backend.callsTo(PATCH)[0]?.body).toEqual({
      name: "Long rope",
      notes: "Frayed",
      quantity: 5,
      hidden_from_party: false,
      reason: "",
    });

    await user.click(
      await screen.findByRole("button", { name: "Move Long rope" }),
    );
    form = within(dialog());
    // The item's current owner is not a destination.
    expect(
      form.getByRole("combobox", { name: "Move to" }).textContent,
    ).toContain("Party pool");
    await chooseOption(dialog(), "Move to", "Wren Ashdown");
    await user.click(form.getByRole("button", { name: "Move Long rope" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(backend.callsTo(MOVE)[0]?.body).toEqual({
      owner: wren.id,
      quantity: 5,
      reason: "",
    });
    const pack = screen.getByRole("region", { name: "Wren Ashdown" });
    expect(await within(pack).findByText("Long rope")).toBeTruthy();

    await user.click(button("Edit Long rope"));
    await user.click(button("Delete Long rope"));
    const confirm = within(screen.getByRole("alertdialog"));
    expect(confirm.getByText("Delete Long rope?")).toBeTruthy();
    expect(confirm.getByText("Its audit history is retained.")).toBeTruthy();
    // The confirmation replaces the edit form and saves nothing.
    expect(screen.queryByRole("dialog")).toBeNull();
    await user.click(confirm.getByRole("button", { name: "Cancel" }));
    expect(backend.callsTo(DELETE)).toHaveLength(0);
    expect(backend.callsTo(PATCH)).toHaveLength(1);

    await user.click(button("Edit Long rope"));
    await user.click(button("Delete Long rope"));
    await user.click(
      within(screen.getByRole("alertdialog")).getByRole("button", {
        name: "Delete item",
      }),
    );
    expect(await findToast("Long rope deleted.")).toBeTruthy();
    expect(backend.callsTo(DELETE)[0]?.params.item_id).toBe(inventoryIds.rope);
    await waitFor(() => expect(screen.queryByText("Long rope")).toBeNull());
  });

  it("offers a retry when the inventory cannot be read", async () => {
    backend = installBackend({ [ITEMS]: { status: 500, detail: "Down." } });
    render(<Panel />);
    expect(
      await screen.findByText("Could not load the inventory"),
    ).toBeTruthy();
    expect(screen.getByText("Down.")).toBeTruthy();
    backend.set(playerStore());
    await userEvent.setup().click(button("Try again"));
    expect(await screen.findByText("Potion of healing")).toBeTruthy();
  });

  it("is the session screen's Inventory tab", async () => {
    const { user } = openApp(
      {
        "GET /lobby": lobby(),
        "GET /campaigns/{campaign_id}/characters": characters,
        ...sessionRoutes,
        ...inventoryStore(),
      },
      `/campaigns/${ids.owned}/session`,
    );
    await user.click(await screen.findByRole("tab", { name: "Inventory" }));
    const pack = await screen.findByRole("region", { name: "Brannoch Stone" });
    expect(within(pack).getByText("Silk rope")).toBeTruthy();
  });
});
