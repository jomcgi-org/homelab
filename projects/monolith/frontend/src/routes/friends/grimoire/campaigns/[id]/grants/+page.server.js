import { error } from "@sveltejs/kit";
import { grimoireJson } from "$lib/server/grimoire-auth.js";

export async function load({ fetch, cookies, params, setHeaders, url }) {
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
  const query = (url?.searchParams.get("q") || "").trim();
  const cursor = url?.searchParams.get("cursor") || "0";
  if (query.length > 120 || !/^\d{1,9}$/.test(cursor))
    error(400, "Invalid knowledge search or cursor.");
  const granted = await grimoireJson(
    fetch,
    cookies,
    `${base}/entities?granted_only=true&limit=100&cursor=${cursor}`,
  );
  const search = query
    ? await grimoireJson(
        fetch,
        cookies,
        `${base}/entities?limit=50&q=${encodeURIComponent(query)}`,
      )
    : { items: [] };
  const entities = [
    ...new Map(
      [...granted.items, ...search.items].map((row) => [row.id, row]),
    ).values(),
  ];
  return {
    campaign,
    characters,
    grants,
    sessions,
    entities,
    query,
    nextCursor: granted.next_cursor,
    searchTruncated: !!search.next_cursor,
  };
}
