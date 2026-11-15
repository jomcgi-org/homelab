export function sheetRolls(derived, kind = "checks", mode = "normal") {
  const suffix = mode === "normal" ? "" : mode;
  if (kind === "attacks") {
    const bonuses = derived?.attack_bonuses;
    return ["melee", "ranged"]
      .filter((attack) => Number.isInteger(bonuses?.[attack]))
      .map((attack) => {
        const bonus = bonuses[attack];
        return {
          label: `${attack[0].toUpperCase()}${attack.slice(1)} attack`,
          formula: `d20${suffix}${bonus >= 0 ? "+" : ""}${bonus}`,
        };
      });
  }
  const bonuses =
    kind === "saves"
      ? derived?.saving_throw_bonuses
      : derived?.ability_modifiers;
  return Object.entries(bonuses || {})
    .filter(([, bonus]) => Number.isInteger(bonus))
    .map(([ability, bonus]) => ({
      label: `${ability[0].toUpperCase()}${ability.slice(1)} ${kind === "saves" ? "save" : "check"}`,
      formula: `d20${suffix}${bonus >= 0 ? "+" : ""}${bonus}`,
    }));
}
