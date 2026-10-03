import { error } from "@sveltejs/kit";
import { grimoireJson } from "$lib/server/grimoire-auth.js";

export async function load({ fetch, cookies, params, setHeaders }) {
  if (process.env.GRIMOIRE_PLAY_ENABLED !== "true")
    error(404, "Session play is not enabled.");
  setHeaders({ "cache-control": "private, no-store" });
  const lobby = await grimoireJson(fetch, cookies, "/lobby");
  const campaign = lobby.campaigns.find(
    (row) => row.id === params.id && row.role === "dm",
  );
  if (!campaign) error(403, "The grants table is for the DM.");
  const base = `/campaigns/${params.id}`;
  const [characters, grants, sessions] = await Promise.all([
    grimoireJson(fetch, cookies, `${base}/characters`),
    grimoireJson(fetch, cookies, `${base}/grants`),
    grimoireJson(fetch, cookies, `${base}/sessions`),
  ]);
  const entities = [];
  let cursor = "";
  do {
    const page = await grimoireJson(
      fetch,
      cookies,
      `${base}/entities?limit=500${cursor ? `&cursor=${cursor}` : ""}`,
    );
    entities.push(...page.items);
    cursor = page.next_cursor;
  } while (cursor);
  return { campaign, characters, grants, sessions, entities };
}
