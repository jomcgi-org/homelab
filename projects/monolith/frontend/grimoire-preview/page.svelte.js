import { writable } from "svelte/store";

export const page = $state({
  url: new URL("https://jomcgi.dev/app/grimoire"),
  params: {},
  route: { id: "/public/app/grimoire" },
  data: {},
  error: null,
});
export const pageStore = writable(page);
export function setPage(url, surface, data = {}) {
  page.url = new URL(url);
  page.params = { book: "fixture-book", id: "fixture-person" };
  page.route = {
    id:
      "/public/app/grimoire" +
      ({
        home: "",
        entity: "/entity/[id]",
        adventure: "/adventure/[id]",
        book: "/book/[book]",
        share: "/chat/s/[id]",
      }[surface] ?? `/${surface}`),
  };
  page.data = data;
  pageStore.set(page);
}
