import { platformJson } from "$lib/server/platform-auth.js";

export async function load({ fetch, cookies, setHeaders }) {
  setHeaders({ "cache-control": "private, no-store" });
  return { user: await platformJson(fetch, cookies, "/self") };
}
