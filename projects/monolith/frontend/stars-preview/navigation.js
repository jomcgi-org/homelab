import { setPage } from "../test/profile-page.svelte.js";
import { data } from "../test/stars/fixtures.js";
export function goto(url) {
  // The nested artifact URL is transport only. Production sees /app/stars.
  const actual = new URL(location.href);
  actual.search = url.search;
  history.replaceState({}, "", actual);
  setPage(url, data);
}
export function invalidateAll() {}
