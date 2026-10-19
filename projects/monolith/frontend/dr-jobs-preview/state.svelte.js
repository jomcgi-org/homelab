import { writable } from "svelte/store";

// Both SvelteKit page APIs share one state, as in the profile preview.
export const page = $state({
  url: new URL("https://preview.invalid/app/dr-jobs"),
  data: {},
  params: {},
  error: null,
  status: 200,
});
export const pageStore = writable(page);
export function setPage(url, data = page.data) {
  page.url = new URL(url);
  page.data = data;
  pageStore.set(page);
}
