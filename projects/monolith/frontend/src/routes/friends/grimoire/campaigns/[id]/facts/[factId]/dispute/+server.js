import { json } from "@sveltejs/kit";
import { grimoireJson } from "$lib/server/grimoire-auth.js";

const UUID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const headers = { "cache-control": "private, no-store" };

export async function POST({ params, fetch, cookies }) {
  if (!UUID.test(params.id) || !UUID.test(params.factId))
    return json({ error: "Fact not found." }, { status: 404, headers });
  try {
    const fact = await grimoireJson(
      fetch,
      cookies,
      `/campaigns/${params.id}/facts/${params.factId}/dispute`,
      { method: "POST" },
    );
    return json(fact, { headers });
  } catch (cause) {
    // Never echo arbitrary backend/provider errors or addressed identifiers.
    return json(
      { error: "The fact could not be disputed." },
      { status: cause.status || 502, headers },
    );
  }
}
