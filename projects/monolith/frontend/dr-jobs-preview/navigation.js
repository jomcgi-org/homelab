import { page, setPage } from "./state.svelte.js";

// Fixture-only observability for the real page's navigation and refresh calls.
// This does not emulate SvelteKit server loads or make any backend requests.
export const calls = { goto: [], invalidations: 0 };
export async function goto(url, options) {
  const target = new URL(url, page.url);
  if (
    target.origin !== page.url.origin ||
    target.pathname !== page.url.pathname
  )
    throw new Error("Fixture navigation must stay on this page");
  calls.goto.push({ url: target.href, options });
  if (typeof window !== "undefined" && location.origin === target.origin)
    history.replaceState({}, "", target);
  setPage(target);
}
export async function invalidateAll() {
  calls.invalidations += 1;
}
