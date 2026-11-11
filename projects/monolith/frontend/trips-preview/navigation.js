import { page, setPage } from "../test/profile-page.svelte.js";
export function goto(url, options = {}) {
  history[options.replaceState ? "replaceState" : "pushState"]({}, "", url);
  setPage(url.href, page.data);
}
export function invalidateAll() {}
