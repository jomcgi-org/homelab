import { writable } from "svelte/store";

export const page = writable({
  url: new URL("https://private.fixture.invalid/factory/execution"),
  data: {},
  error: null,
  status: 200,
});

export function executionLocation(view, search = "") {
  const url = new URL(
    `/factory/execution${view === "drain" ? "/drain" : ""}`,
    "https://private.fixture.invalid",
  );
  url.search = search;
  page.update((value) => ({ ...value, url }));
}
