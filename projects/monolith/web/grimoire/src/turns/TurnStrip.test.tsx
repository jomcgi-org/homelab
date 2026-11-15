import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { TurnOrder } from "../api/session";
import { dmOrder } from "../test/fixtures/turns";
import { renderPanel } from "./renderPanel";
import { TurnStrip } from "./TurnStrip";

const SECRET = "The Pale Warden";
// The player projection of the same order.
const playerOrder: TurnOrder = {
  ...dmOrder,
  entries: [
    dmOrder.entries![0]!,
    dmOrder.entries![1]!,
    { label: "???", player_character_id: null, hidden: true },
  ],
};

const items = () =>
  within(screen.getByRole("region", { name: "Turn order" })).getAllByRole(
    "listitem",
  );
const active = () =>
  items().filter((item) => item.getAttribute("aria-current") === "step");

describe("TurnStrip", () => {
  it("lists the entries in order with the round", () => {
    renderPanel(<TurnStrip order={dmOrder} dm />);
    expect(items().map((item) => item.textContent)).toEqual([
      "Brannoch StoneActive turn",
      "Cellar rats",
      `${SECRET}hidden`,
    ]);
    expect(screen.getByText("Round 2")).toBeTruthy();
  });

  it("marks only the active entry, in text and for assistive technology", () => {
    renderPanel(<TurnStrip order={{ ...dmOrder, active_index: 1 }} dm />);
    expect(active()).toHaveLength(1);
    expect(items().indexOf(active()[0]!)).toBe(1);
    expect(screen.getAllByText("Active turn")).toHaveLength(1);
    expect(within(active()[0]!).getByText("Active turn")).toBeTruthy();
  });

  it("marks nothing when no entry is active", () => {
    renderPanel(<TurnStrip order={{ ...dmOrder, active_index: null }} dm />);
    expect(active()).toHaveLength(0);
    expect(screen.queryByText("Active turn")).toBeNull();
  });

  it("shows a player the masked entry with no hidden badge", () => {
    renderPanel(<TurnStrip order={playerOrder} dm={false} />);
    expect(items()[2]!.textContent).toBe("???");
    expect(screen.queryByText("hidden")).toBeNull();
  });

  it("keeps a secret name off a player's page even when handed the DM view", () => {
    renderPanel(
      <TurnStrip order={{ ...dmOrder, active_index: 2 }} dm={false} />,
    );
    expect(items()[2]!.textContent).toBe("???Active turn");
    expect(document.body.innerHTML).not.toContain(SECRET);
    expect(screen.queryByText("hidden")).toBeNull();
  });

  it.each([
    ["no order", null],
    ["an order with no entries", { round: 1, active_index: null, entries: [] }],
    ["an order without entries", {}],
  ])("renders nothing for %s", (_name, order) => {
    renderPanel(<TurnStrip order={order as TurnOrder} dm={false} />);
    expect(screen.queryByRole("region", { name: "Turn order" })).toBeNull();
    expect(screen.queryByText(/Round/)).toBeNull();
  });
});
