import { sessionState } from "$lib/server/grimoire-session.js";

export async function load({ fetch, cookies, params, setHeaders, url }) {
  setHeaders({ "cache-control": "private, no-store" });
  return sessionState(
    fetch,
    cookies,
    params.id,
    url?.searchParams.get("session"),
  );
}
