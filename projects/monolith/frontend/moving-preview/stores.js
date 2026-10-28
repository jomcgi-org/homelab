import { writable } from "svelte/store";
export const page = writable({
  url: new URL("https://friends.example.test/moving"),
  status: 200,
  error: null,
  data: {},
});
