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
  it("builds melee and ranged attack chips from derived bonuses", () => {
    expect(
      sheetRolls({ attack_bonuses: { melee: 6, ranged: 5 } }, "attacks"),
    ).toEqual([
      { label: "Melee attack", formula: "d20+6" },
      { label: "Ranged attack", formula: "d20+5" },
    ]);
  });
  it("applies advantage and signed bonuses to attack chips", () => {
    expect(
      sheetRolls({ attack_bonuses: { melee: -1, ranged: 5 } }, "attacks", "dis"),
    ).toEqual([
      { label: "Melee attack", formula: "d20dis-1" },
      { label: "Ranged attack", formula: "d20dis+5" },
    ]);
  });
  it("offers no attack chips when derived bonuses are missing", () => {
    expect(sheetRolls({ ability_modifiers: { strength: 3 } }, "attacks")).toEqual(
      [],
    );
    expect(sheetRolls(null, "attacks")).toEqual([]);
  });
  it("offers no rolls without an approved sheet", () => {
    expect(sheetRolls(null)).toEqual([]);
  });
});
