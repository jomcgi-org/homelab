import { fail } from "@sveltejs/kit";
import { grimoireJson } from "$lib/server/grimoire-auth.js";

const UUID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
function id(data, name) {
  const value = data.get(name);
  if (!UUID.test(value)) throw new Error("Invalid selection.");
  return value;
}

export async function load({ fetch, cookies, setHeaders }) {
  setHeaders({ "cache-control": "private, no-store" });
  const lobby = await grimoireJson(fetch, cookies, "/lobby");
  const campaigns = await Promise.all(
    lobby.campaigns.map(async (campaign) => {
      if (!campaign.is_owner) return campaign;
      const [members, invitations] = await Promise.all([
        grimoireJson(fetch, cookies, `/campaigns/${campaign.id}/members`),
        grimoireJson(fetch, cookies, `/campaigns/${campaign.id}/invitations`),
      ]);
      return { ...campaign, members, invitations };
    }),
  );
  return { ...lobby, campaigns };
}

function action(run) {
  return async ({ request, fetch, cookies }) => {
    const data = await request.formData();
    const api = (path, method = "POST", body) =>
      grimoireJson(fetch, cookies, path, {
        method,
        ...(body
          ? {
              headers: { "content-type": "application/json" },
              body: JSON.stringify(body),
            }
          : {}),
      });
    try {
      await run(data, api);
      return { ok: true };
    } catch (error) {
      return fail(400, { error: error.message });
    }
  };
}

export const actions = {
  create: action((data, api) =>
    api("/campaigns", "POST", { name: String(data.get("name") || "") }),
  ),
  invite: action((data, api) =>
    api(`/campaigns/${id(data, "campaign_id")}/invitations`, "POST", {
      email: String(data.get("email") || ""),
    }),
  ),
  accept: action((data, api) =>
    api(`/invitations/${id(data, "invitation_id")}/accept`),
  ),
  decline: action((data, api) =>
    api(`/invitations/${id(data, "invitation_id")}/decline`),
  ),
  cancel: action((data, api) =>
    api(
      `/campaigns/${id(data, "campaign_id")}/invitations/${id(data, "invitation_id")}`,
      "DELETE",
    ),
  ),
  remove: action((data, api) =>
    api(
      `/campaigns/${id(data, "campaign_id")}/members/${id(data, "member_id")}`,
      "DELETE",
    ),
  ),
};
