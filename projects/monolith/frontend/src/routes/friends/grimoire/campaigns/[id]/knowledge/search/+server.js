import { json } from "@sveltejs/kit";
import { grimoireJson } from "$lib/server/grimoire-auth.js";

const UUID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const headers = { "cache-control": "private, no-store" };

export async function GET({ params, url, fetch, cookies }) {
  if (!UUID.test(params.id))
    return json({ error: "Campaign not found." }, { status: 404, headers });
  const q = (url.searchParams.get("q") || "").trim();
  if (!q || q.length > 200)
    return json(
      { error: "Enter a search of 1 to 200 characters." },
      { status: 400, headers },
    );
  const query = new URLSearchParams({ q });
  // Keep the backend's default and validation for the optional result limit.
  if (url.searchParams.has("k")) query.set("k", url.searchParams.get("k"));
  try {
    const results = await grimoireJson(
      fetch,
      cookies,
      `/campaigns/${params.id}/knowledge/search?${query}`,
    );
    return json(results, { headers });
  } catch (cause) {
    return json(
      { error: cause.message || "Knowledge search is unavailable." },
      { status: cause.status || 502, headers },
    );
  }
}
