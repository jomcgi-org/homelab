import { page, pageStore } from "../test/profile-page.svelte.js";

export function replaceState(url, state) {
  const target = new URL(url, location.href);
  if (
    target.origin !== location.origin ||
    target.pathname !== page.url.pathname
  ) {
    throw new Error("Sheet fixture navigation must stay on this page");
  }
  const entry = new URL(location.href);
  entry.search = target.search;
  history.replaceState(JSON.parse(JSON.stringify(state)), "", entry);
  page.url = target;
  pageStore.set(page);
}
