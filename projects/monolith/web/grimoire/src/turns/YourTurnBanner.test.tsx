import { screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import type { TurnOrder } from "../api/session";
import { renderPanel } from "./renderPanel";
import { YourTurnBanner } from "./YourTurnBanner";

const mine = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const other = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const entries = [
  { label: "Aria", player_character_id: mine, initiative: 19, hidden: false },
  { label: "Bram", player_character_id: other, initiative: 15, hidden: false },
  { label: "???", player_character_id: null, hidden: true },
  { label: "Goblin", player_character_id: null, initiative: 8, hidden: false },
];
const at = (active_index: number | null): TurnOrder => ({
  round: 1,
  active_index,
  entries,
});
/** The shared live regions Astryx announces through. They outlive a test. */
const regions = () => [
  ...document.querySelectorAll("[data-astryx-live-region]"),
];
const announced = () =>
  regions()
    .map((region) => region.textContent)
    .join("");
const banner = () =>
  screen
    .queryAllByText("Your turn")
    .find((element) => !element.closest("[data-astryx-live-region]")) ?? null;

beforeEach(() => {
  for (const region of regions()) region.textContent = "";
});

describe("YourTurnBanner", () => {
  it("shows and announces the viewer's own turn", async () => {
    renderPanel(<YourTurnBanner order={at(0)} characterIds={[mine]} />);
    expect(banner()).toBeTruthy();
    await waitFor(() => expect(announced()).toContain("Your turn"));
  });

  it("announces when the turn arrives on a later poll", async () => {
    const { rerender } = renderPanel(
      <YourTurnBanner order={at(1)} characterIds={[mine]} />,
    );
    expect(banner()).toBeNull();
    expect(announced()).not.toContain("Your turn");
    rerender(<YourTurnBanner order={at(0)} characterIds={[mine]} />);
    expect(banner()).toBeTruthy();
    await waitFor(() => expect(announced()).toContain("Your turn"));
  });

  it.each([
    ["another player's character is active", at(1), [mine]],
    ["a masked NPC is active", at(2), [mine]],
    ["a plain NPC is active", at(3), [mine]],
    ["no entry is active", at(null), [mine]],
    ["there is no order", null, [mine]],
    ["the viewer owns no character in the order", at(0), [other]],
    [
      "a hidden entry names the viewer's character",
      {
        round: 1,
        active_index: 0,
        entries: [{ ...entries[0]!, hidden: true }],
      },
      [mine],
    ],
  ])("stays silent when %s", async (_name, order, characterIds) => {
    renderPanel(
      <YourTurnBanner order={order as TurnOrder} characterIds={characterIds} />,
    );
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(banner()).toBeNull();
    expect(announced()).not.toContain("Your turn");
  });
});
