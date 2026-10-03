import { sessionState } from "$lib/server/grimoire-session.js";
import { error } from "@sveltejs/kit";

export async function load({ fetch, cookies, params, setHeaders }) {
  if (process.env.GRIMOIRE_PLAY_ENABLED !== "true")
    error(404, "Session play is not enabled.");
  setHeaders({ "cache-control": "private, no-store" });
  return sessionState(fetch, cookies, params.id);
}
