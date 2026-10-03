export function sheetRolls(derived, kind = "checks", mode = "normal") {
  const bonuses =
    kind === "saves"
      ? derived?.saving_throw_bonuses
      : derived?.ability_modifiers;
  const suffix = mode === "normal" ? "" : mode;
  return Object.entries(bonuses || {})
    .filter(([, bonus]) => Number.isInteger(bonus))
    .map(([ability, bonus]) => ({
      label: `${ability[0].toUpperCase()}${ability.slice(1)} ${kind === "saves" ? "save" : "check"}`,
      formula: `d20${suffix}${bonus >= 0 ? "+" : ""}${bonus}`,
    }));
}
