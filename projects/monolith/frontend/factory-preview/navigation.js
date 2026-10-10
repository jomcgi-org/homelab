import { page } from "./state.svelte.js";

export function replaceState(url, state) {
  const target = new URL(url, location.href);
  if (
    target.origin !== location.origin ||
    target.pathname !== location.pathname
  ) {
    throw new Error("Fixture navigation must remain in the static artifact");
  }
  const physical = new URL(location.href);
  for (const key of ["q", "state", "type", "sort", "page"]) {
    if (target.searchParams.has(key))
      physical.searchParams.set(key, target.searchParams.get(key));
    else physical.searchParams.delete(key);
  }
  history.replaceState(JSON.parse(JSON.stringify(state)), "", physical);
  const virtual = new URL(page.url);
  virtual.search = target.search;
  page.url = virtual;
}

// Fixtures have no origin. The production polling effect is still mounted,
// but revalidation cannot fetch live data or leak browsing state.
export async function invalidateAll() {}
