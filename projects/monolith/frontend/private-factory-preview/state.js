import { writable } from "svelte/store";
import { routes } from "./fixtures.js";

export const page = writable({
  url: new URL("https://private.example.test/factory"),
  data: {},
  error: null,
});
export function setRoute(view, query = "") {
  page.set({
    url: new URL(`https://private.example.test${routes[view]}${query}`),
    data: {},
    error: null,
  });
}
export async function goto(url, options) {
  page.update((value) => ({ ...value, url: new URL(url, value.url) }));
  window.__privateFixture.navigation.push({ url, options });
}
