import { describe, expect, it } from "vitest";
import { sheetRolls } from "./sheet-rolls.js";

describe("approved sheet rolls", () => {
  it("uses persisted bonuses including negative modifiers", () => {
    expect(
      sheetRolls({
        ability_scores: { strength: 20 },
        ability_modifiers: { strength: 2, dexterity: -1 },
      }),
    ).toEqual([
      { label: "Strength check", formula: "d20+2" },
      { label: "Dexterity check", formula: "d20-1" },
    ]);
  });
  it("uses approved saves with advantage", () => {
    expect(
      sheetRolls({ saving_throw_bonuses: { strength: 4 } }, "saves", "adv"),
    ).toEqual([{ label: "Strength save", formula: "d20adv+4" }]);
  });
  it("offers no rolls without an approved sheet", () => {
    expect(sheetRolls(null)).toEqual([]);
  });
});
