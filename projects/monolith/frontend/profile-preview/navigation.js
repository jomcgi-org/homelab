import { setPage, page } from "../test/profile-page.svelte.js";
export async function goto(url) {
  history.replaceState({}, "", url);
  setPage(url.href, page.data);
}
export async function invalidateAll() {
  // The fixture retains its synthetic snapshot; server loads run in Vitest.
  window.dispatchEvent(new Event("fixture-snapshot-refresh"));
}
