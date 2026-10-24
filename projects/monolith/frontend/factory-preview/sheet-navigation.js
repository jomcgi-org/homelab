import { page } from "./sheet-state.svelte.js";
export function replaceState(url, state) {
  const physical = new URL(location.href);
  const target = new URL(url, physical);
  physical.hash = target.hash;
  history.replaceState(JSON.parse(JSON.stringify(state)), "", physical);
  const virtual = new URL(page.url);
  virtual.hash = target.hash;
  if (!String(url).startsWith("#")) virtual.search = target.search;
  page.url = virtual;
}
export async function invalidateAll() {
  window.__fixturePolls++;
}
