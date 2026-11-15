import { writable } from "svelte/store";
export const page = writable({
  url: new URL("https://private.fixture.invalid/review"),
  data: {},
  status: 200,
  error: null,
});
export const navigationCalls = [];
export async function goto(url, options) {
  navigationCalls.push({ url: String(url), options });
  page.update((value) => ({ ...value, url: new URL(url, value.url) }));
}
export async function invalidateAll() {
  navigationCalls.push({ invalidateAll: true });
}
// Only data-free SvelteKit ActionResults are supplied by this harness.
// Production uses SvelteKit's deserialize, including its devalue decoder.
export function deserialize(value) {
  const result = JSON.parse(value);
  if (result.data) throw new Error("Fixture ActionResults must be data-free");
  return result;
}
