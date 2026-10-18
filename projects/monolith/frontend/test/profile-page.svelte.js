import { writable } from "svelte/store";

// A single fixture page drives both SvelteKit APIs used by the real layouts.
export const page = $state({
  url: new URL("https://jomcgi.dev/"),
  data: {},
  params: {},
  error: null,
  status: 200,
});
export const pageStore = writable(page);
export function setPage(url, data = {}, error = null) {
  page.url = new URL(url);
  page.data = data;
  page.error = error;
  page.status = error ? 404 : 200;
  pageStore.set(page);
}
