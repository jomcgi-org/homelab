import type { CharacterSheetV1 } from "../../api/schema";
import { ABILITIES, BOUNDS, type Ability } from "../../api/sheets";

export type SheetField = "ancestry" | "class_name" | "level" | Ability;
export const sheetFields: ReadonlyArray<{ name: SheetField; label: string }> = [
  { name: "ancestry", label: "Ancestry" },
  { name: "class_name", label: "Class" },
  { name: "level", label: "Level" },
  ...ABILITIES,
];

export const fieldValue = (sheet: CharacterSheetV1, field: SheetField) =>
  field === "ancestry" || field === "class_name" || field === "level"
    ? sheet[field]
    : sheet.ability_scores[field];

const withField = (
  sheet: CharacterSheetV1,
  field: SheetField,
  value: string | number,
): CharacterSheetV1 => {
  if (field === "ancestry" || field === "class_name")
    return { ...sheet, [field]: String(value) };
  if (field === "level") return { ...sheet, level: Number(value) };
  return {
    ...sheet,
    ability_scores: { ...sheet.ability_scores, [field]: Number(value) },
  };
};

/** Merge independent edits; overlapping values require an explicit choice. */
export function mergeSheet(
  base: CharacterSheetV1,
  local: CharacterSheetV1,
  remote: CharacterSheetV1,
  unresolved: SheetField[],
  typing: SheetField[],
) {
  let sheet = remote;
  const conflicts: SheetField[] = [];
  for (const { name } of sheetFields) {
    const before = fieldValue(base, name);
    const mine = fieldValue(local, name);
    const theirs = fieldValue(remote, name);
    // A focused NumberInput still owns text until blur. Even if that text
    // equals the old base, require a choice when its saved value changes.
    const localChanged = mine !== before || typing.includes(name);
    if (localChanged) sheet = withField(sheet, name, mine);
    if (
      mine !== theirs &&
      localChanged &&
      (unresolved.includes(name) || theirs !== before)
    )
      conflicts.push(name);
  }
  return { sheet, conflicts };
}

export function takeSavedFields(
  local: CharacterSheetV1,
  saved: CharacterSheetV1,
  fields: SheetField[],
) {
  return fields.reduce(
    (sheet, field) => withField(sheet, field, fieldValue(saved, field)),
    local,
  );
}

export type NumericText = Partial<Record<"level" | Ability, string>>;

/** NumberInput commits on blur; retain valid in-progress edits during a poll. */
export function withNumericText(sheet: CharacterSheetV1, text: NumericText) {
  let result = sheet;
  for (const { name } of sheetFields) {
    if (name === "ancestry" || name === "class_name") continue;
    const raw = text[name];
    const value = Number(raw);
    const bounds = name === "level" ? BOUNDS.level : BOUNDS.score;
    if (
      raw?.trim() &&
      Number.isInteger(value) &&
      value >= bounds.min &&
      value <= bounds.max
    )
      result = withField(result, name, value);
  }
  return result;
}

export const describeSheet = (sheet: CharacterSheetV1, text: NumericText) =>
  sheetFields
    .map(({ name, label }) => {
      const raw =
        name === "ancestry" || name === "class_name" ? undefined : text[name];
      return `${label}: ${raw ?? fieldValue(sheet, name)}`;
    })
    .join("\n");
