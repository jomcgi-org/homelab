import { browser } from "$app/environment";
import {
  overviewClock,
  overviewDeadline,
} from "$lib/public/notes/overview-lease.js";

// Render the current public snapshot overview on the server. The interactive
// canvas and complete graph payload load progressively in the browser.
export const ssr = true;

export function load({ data }) {
  if (!browser) return { ...data, overviewDeadline: null };
  return {
    ...data,
    overviewDeadline: overviewDeadline(data.overview, performance),
    overviewClock: overviewClock(data.overview, performance),
  };
}
