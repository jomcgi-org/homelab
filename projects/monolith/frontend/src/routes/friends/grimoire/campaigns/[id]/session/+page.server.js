import { sessionState } from "$lib/server/grimoire-session.js";
import { error } from "@sveltejs/kit";

export async function load({ fetch, cookies, params, setHeaders, url }) {
  if (process.env.GRIMOIRE_PLAY_ENABLED !== "true")
    error(404, "Session play is not enabled.");
  setHeaders({ "cache-control": "private, no-store" });
  const state = await sessionState(
    fetch,
    cookies,
    params.id,
    url?.searchParams.get("session"),
  );
  return {
    ...state,
    rolloutRecoveryEnabled:
      process.env.GRIMOIRE_ROLLOUT_RECOVERY_ENABLED === "true",
  };
}
