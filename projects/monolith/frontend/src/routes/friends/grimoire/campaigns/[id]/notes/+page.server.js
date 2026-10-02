import { error, fail, isRedirect } from "@sveltejs/kit";
import { grimoireJson } from "$lib/server/grimoire-auth.js";

const UUID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

function campaignId(params) {
  if (!UUID.test(params.id)) error(404, "Campaign not found.");
  return params.id;
}

function id(data, name) {
  const value = data.get(name);
  if (typeof value !== "string" || !UUID.test(value))
    throw new Error("Invalid selection.");
  return value;
}

export async function load({ params, url, fetch, cookies, setHeaders }) {
  setHeaders({ "cache-control": "private, no-store" });
  const campaign = campaignId(params);
  const kind = url.searchParams.get("kind") === "party" ? "party" : "character";
  const q = url.searchParams.get("q") || "";
  const lobby = await grimoireJson(fetch, cookies, "/lobby");
  const membership = lobby.campaigns.find(
    (row) => row.id.toLowerCase() === campaign.toLowerCase(),
  );
  if (!membership) error(404, "Campaign not found.");
  // A failed read rejects the page load. Never substitute an empty notes list.
  const notes = await grimoireJson(
    fetch,
    cookies,
    `/campaigns/${campaign}/notes?${new URLSearchParams({ kind, q })}`,
  );
  return { campaign: membership, notes, kind, q };
}

function text(data) {
  return {
    title: String(data.get("title") || "").trim(),
    markdown: String(data.get("markdown") || ""),
  };
}

function sharing(data) {
  if (!data.has("dm_readable")) return {};
  const value = data.get("dm_readable");
  if (value !== "true" && value !== "false")
    throw new Error("Invalid DM-sharing choice.");
  return { dm_readable: value === "true" };
}

function action(run) {
  return async ({ params, request, fetch, cookies }) => {
    const campaign = campaignId(params);
    try {
      const data = await request.formData();
      const selected = id(data, "campaign_id");
      if (selected.toLowerCase() !== campaign.toLowerCase())
        throw new Error("Invalid campaign selection.");
      const api = (path, method, body) =>
        grimoireJson(fetch, cookies, `/campaigns/${campaign}${path}`, {
          method,
          ...(body
            ? {
                headers: { "content-type": "application/json" },
                body: JSON.stringify(body),
              }
            : {}),
        });
      await run(data, api);
      return { ok: true };
    } catch (cause) {
      if (isRedirect(cause)) throw cause;
      return fail(400, { error: cause.message });
    }
  };
}

export const actions = {
  create: action((data, api) => {
    const kind = data.get("kind");
    if (kind !== "character" && kind !== "party")
      throw new Error("Invalid note kind.");
    return api("/notes", "POST", { kind, ...text(data), ...sharing(data) });
  }),
  update: action((data, api) =>
    api(`/notes/${id(data, "note_id")}`, "PATCH", {
      ...text(data),
      ...sharing(data),
    }),
  ),
  delete: action((data, api) => api(`/notes/${id(data, "note_id")}`, "DELETE")),
};
