// The trip + points come from [slug]/+layout.server.js (shared across the
// summary, timeline and day pages). This load only resolves the day number from
// the route param; it merges into the layout data so the page sees
// { trip, points, dayNumber }.
export function load({ params }) {
  return { dayNumber: Number.parseInt(params.day, 10) };
}
