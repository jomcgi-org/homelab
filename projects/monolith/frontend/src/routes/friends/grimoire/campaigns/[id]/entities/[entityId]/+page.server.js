import { error } from "@sveltejs/kit";
import { grimoireJson } from "$lib/server/grimoire-auth.js";

export async function load({ fetch, cookies, params, setHeaders }) {
  setHeaders({ "cache-control": "private, no-store" });
  if (!/^[0-9a-f-]{36}$/i.test(params.entityId))
    error(404, "Knowledge entry not found.");
  try {
    return {
      entity: await grimoireJson(
        fetch,
        cookies,
        `/campaigns/${params.id}/entities/${params.entityId}`,
      ),
      campaignId: params.id,
    };
  } catch {
    error(404, "This knowledge entry is not available to you.");
  }
}
