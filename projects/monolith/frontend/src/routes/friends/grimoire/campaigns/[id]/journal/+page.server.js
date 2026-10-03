import { error } from "@sveltejs/kit";
import { grimoireJson } from "$lib/server/grimoire-auth.js";

const UUID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export async function load({ params, url, fetch, cookies, setHeaders }) {
  setHeaders({ "cache-control": "private, no-store" });
  if (!UUID.test(params.id)) error(404, "Campaign not found.");
  const campaignId = params.id;
  const view = url.searchParams.get("view") === "party" ? "party" : "mine";
  const cursor = url.searchParams.get("cursor");
  const lobby = await grimoireJson(fetch, cookies, "/lobby");
  const membership = lobby.campaigns.find(
    (row) => row.id.toLowerCase() === campaignId.toLowerCase(),
  );
  if (!membership) error(404, "Campaign not found.");
  const query = new URLSearchParams({ view });
  if (cursor !== null) query.set("cursor", cursor);
  // Fail closed, including when play is disabled. Never invent an empty journal.
  const journal = await grimoireJson(
    fetch,
    cookies,
    `/campaigns/${campaignId}/journal?${query}`,
  );
  return { campaign: membership, journal, view };
}
