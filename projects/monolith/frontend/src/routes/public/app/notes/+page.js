import { browser } from "$app/environment";
import { overviewDeadline } from "$lib/public/notes/overview-lease.js";

// Render the current public snapshot overview on the server. The interactive
// canvas and complete graph payload load progressively in the browser.
export const ssr = true;

export function load({ data }) {
  return {
    ...data,
    overviewDeadline: browser
      ? overviewDeadline(data.overview, performance)
      : null,
  };
}
