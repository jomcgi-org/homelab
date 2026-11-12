// Mirrors derive_sheet in projects/monolith/grimoire/sheets.py, so the sheet
// form can show the numbers before a draft is saved. The server's result is
// the one that counts: change both sides together.

/** @typedef {"strength" | "dexterity" | "constitution" | "intelligence" | "wisdom" | "charisma"} Ability */

/** @type {Ability[]} */
export const ABILITY_NAMES = [
  "strength",
  "dexterity",
  "constitution",
  "intelligence",
  "wisdom",
  "charisma",
];

/** The refusals derive_sheet raises for a class entry it cannot calculate from. */
export const REFUSALS = {
  hitDie: "class corpus entry has no supported hit die",
  saves: "class corpus entry has no supported saving throws",
};

// Python's \b counts any Unicode letter or digit as a word character; these
// lookarounds say the same thing where JavaScript's \b is ASCII-only.
const WORD = "[\\p{L}\\p{N}_]";
// _HIT_DIE: a standalone d6, d8, d10 or d12. "1d8" and "d20" do not count.
const HIT_DIE = new RegExp(`(?<!${WORD})d(6|8|10|12)(?!${WORD})`, "iu");
const SAVE = Object.fromEntries(
  ABILITY_NAMES.map((ability) => [
    ability,
    new RegExp(
      `(?<!${WORD})(?:${ability}|${ability.slice(0, 3)})(?!${WORD})`,
      "u",
    ),
  ]),
);

/**
 * `_class_hit_die`: the die size a class entry names, or null.
 * @param {unknown} raw
 * @returns {number | null}
 */
export function parseHitDie(raw) {
  if (typeof raw !== "string") return null;
  const match = HIT_DIE.exec(raw.trim());
  return match ? Number(match[1]) : null;
}

/**
 * `_class_saves`: the abilities a class entry names by full or three-letter
 * name, in sheet order, or null when it names none.
 * @param {unknown} raw
 * @returns {Ability[] | null}
 */
export function parseSaves(raw) {
  if (typeof raw !== "string" || !raw.trim()) return null;
  const normalized = raw.toLowerCase();
  const saves = ABILITY_NAMES.filter((ability) =>
    SAVE[ability].test(normalized),
  );
  return saves.length ? saves : null;
}

/**
 * Floor division, as Python's `//`.
 * @param {number} score
 */
export const abilityModifier = (score) => Math.floor((score - 10) / 2);

/**
 * @param {(ability: Ability) => number} value
 * @returns {Record<Ability, number>}
 */
const byAbility = (value) =>
  /** @type {Record<Ability, number>} */ (
    Object.fromEntries(
      ABILITY_NAMES.map((ability) => [ability, value(ability)]),
    )
  );

/** @param {number} level */
export const proficiencyBonus = (level) => 2 + Math.floor((level - 1) / 4);

/**
 * What derive_sheet would calculate from these base facts. The values that
 * need a class are null until one is chosen, and stay null with `refusal`
 * set when the chosen class entry lacks a usable hit die or saving throws.
 *
 * @param {{ level: number, ability_scores: Record<Ability, number> }} sheet
 * @param {{ hit_die?: unknown, saves?: unknown } | null} characterClass
 */
export function previewSheet(sheet, characterClass) {
  const modifiers = byAbility((ability) =>
    abilityModifier(sheet.ability_scores[ability]),
  );
  const proficiency = proficiencyBonus(sheet.level);
  const preview = {
    ability_modifiers: modifiers,
    proficiency_bonus: proficiency,
    attack_bonuses: {
      melee: modifiers.strength + proficiency,
      ranged: modifiers.dexterity + proficiency,
    },
    unarmored_armor_class: 10 + modifiers.dexterity,
    /** @type {string | null} */
    refusal: null,
    /** @type {Ability[] | null} */
    proficient_saves: null,
    /** @type {Record<Ability, number> | null} */
    saving_throw_bonuses: null,
    /** @type {number | null} */
    max_hit_points: null,
    /** @type {string | null} */
    hit_die: null,
  };
  if (!characterClass) return preview;

  // The server checks the hit die first, so that refusal wins.
  const hitDie = parseHitDie(characterClass.hit_die);
  if (hitDie === null) return { ...preview, refusal: REFUSALS.hitDie };
  const saves = parseSaves(characterClass.saves);
  if (saves === null) return { ...preview, refusal: REFUSALS.saves };

  const constitution = modifiers.constitution;
  const firstLevel = Math.max(1, hitDie + constitution);
  const laterLevel = Math.max(1, Math.floor(hitDie / 2) + 1 + constitution);
  return {
    ...preview,
    proficient_saves: saves,
    saving_throw_bonuses: byAbility(
      (ability) =>
        modifiers[ability] + (saves.includes(ability) ? proficiency : 0),
    ),
    max_hit_points: firstLevel + (sheet.level - 1) * laterLevel,
    hit_die: `d${hitDie}`,
  };
}
