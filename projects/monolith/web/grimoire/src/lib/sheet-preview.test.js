import { describe, expect, it } from "vitest";
import {
  abilityModifier,
  parseHitDie,
  parseSaves,
  previewSheet,
  proficiencyBonus,
  REFUSALS,
} from "./sheet-preview.js";

// The sheet and class grimoire/sheets_test.py derives from.
const sheet = (level = 5) => ({
  level,
  ability_scores: {
    strength: 16,
    dexterity: 14,
    constitution: 14,
    intelligence: 10,
    wisdom: 12,
    charisma: 8,
  },
});
const fighter = { hit_die: "d10", saves: "Strength, Constitution" };

describe("sheet preview", () => {
  it("matches the values sheets_test.py pins for a level 5 fighter", () => {
    const preview = previewSheet(sheet(), fighter);
    expect(preview.proficiency_bonus).toBe(3);
    expect(preview.ability_modifiers.strength).toBe(3);
    expect(preview.saving_throw_bonuses.strength).toBe(6);
    expect(preview.saving_throw_bonuses.wisdom).toBe(1);
    expect(preview.attack_bonuses).toEqual({ melee: 6, ranged: 5 });
    expect(preview.unarmored_armor_class).toBe(12);
    expect(preview.max_hit_points).toBe(44);
    expect(preview.hit_die).toBe("d10");
    expect(preview.proficient_saves).toEqual(["strength", "constitution"]);
    expect(preview.refusal).toBeNull();
  });

  it("rounds modifiers down, as Python floor division does", () => {
    expect([3, 8, 9, 10, 11, 12, 19, 20].map(abilityModifier)).toEqual([
      -4, -1, -1, 0, 0, 1, 4, 5,
    ]);
  });

  it("raises proficiency every four levels", () => {
    expect([1, 4, 5, 8, 9, 12, 13, 16, 17, 20].map(proficiencyBonus)).toEqual([
      2, 2, 3, 3, 4, 4, 5, 5, 6, 6,
    ]);
  });

  it("gives at least one hit point a level", () => {
    const frail = {
      level: 3,
      ability_scores: { ...sheet().ability_scores, constitution: 3 },
    };
    // d6 with -4: max(1, 2) at first level, then max(1, 0) twice.
    expect(
      previewSheet(frail, { hit_die: "d6", saves: "int" }).max_hit_points,
    ).toBe(4);
  });

  it("uses the full die at first level and half plus one after", () => {
    expect(previewSheet(sheet(1), fighter).max_hit_points).toBe(12);
    expect(previewSheet(sheet(2), fighter).max_hit_points).toBe(20);
  });

  it("reads a hit die only when it stands alone", () => {
    expect(parseHitDie("d10")).toBe(10);
    expect(parseHitDie("  D8 per level ")).toBe(8);
    expect(parseHitDie("Hit die: d12.")).toBe(12);
    expect(parseHitDie("d6/d8")).toBe(6);
    expect(parseHitDie("1d8")).toBeNull();
    expect(parseHitDie("d100")).toBeNull();
    expect(parseHitDie("d20")).toBeNull();
    expect(parseHitDie("éd8")).toBeNull();
    expect(parseHitDie("")).toBeNull();
    expect(parseHitDie(10)).toBeNull();
    expect(parseHitDie(null)).toBeNull();
  });

  it("reads saves by full or three-letter ability name", () => {
    expect(parseSaves("Strength, Constitution")).toEqual([
      "strength",
      "constitution",
    ]);
    expect(parseSaves("WIS and CHA")).toEqual(["wisdom", "charisma"]);
    expect(parseSaves("Charisma; dex")).toEqual(["dexterity", "charisma"]);
    expect(parseSaves("strengths, wise, intellect")).toBeNull();
    expect(parseSaves("   ")).toBeNull();
    expect(parseSaves(["strength"])).toBeNull();
    expect(parseSaves(undefined)).toBeNull();
  });

  it("refuses as the server does when the class entry cannot be used", () => {
    const noDie = previewSheet(sheet(), { hit_die: "1d8", saves: "str" });
    expect(noDie.refusal).toBe(REFUSALS.hitDie);
    expect(noDie.max_hit_points).toBeNull();
    expect(noDie.saving_throw_bonuses).toBeNull();
    expect(previewSheet(sheet(), { hit_die: "d8", saves: null }).refusal).toBe(
      REFUSALS.saves,
    );
    // The hit die is checked first.
    expect(previewSheet(sheet(), {}).refusal).toBe(REFUSALS.hitDie);
  });

  it("calculates what needs no class before one is chosen", () => {
    const preview = previewSheet(sheet(), null);
    expect(preview.proficiency_bonus).toBe(3);
    expect(preview.unarmored_armor_class).toBe(12);
    expect(preview.max_hit_points).toBeNull();
    expect(preview.hit_die).toBeNull();
    expect(preview.refusal).toBeNull();
  });
});
