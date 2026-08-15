/**
 * Period computation for the private tier's time-of-day palette.
 * Period boundaries must remain in sync with the agents console and the
 * landing page's `+page.svelte`. The landing page's `greeting` message uses
 * different boundaries and is a separate concern.
 */

/**
 * Compute the current period from an hour value.
 * @param {number} hour - Hour value from 0-23
 * @returns {"dawn" | "day" | "dusk" | "night"} The period for the hour
 */
export function periodForHour(hour) {
  if (hour >= 5 && hour < 11) return "dawn";
  if (hour >= 11 && hour < 17) return "day";
  if (hour >= 17 && hour < 22) return "dusk";
  return "night";
}
