import { fail, isRedirect, redirect } from "@sveltejs/kit";
import { grimoireJson } from "$lib/server/grimoire-auth.js";

const UUID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
function id(data, name) {
  const value = data.get(name);
  if (!UUID.test(value)) throw new Error("Invalid selection.");
  return value;
}

function characterName(data) {
  const name = String(data.get("name") || "").trim();
  if (!name || name.length > 120)
    throw new Error("Character name must be between 1 and 120 characters.");
  return name;
}

export async function load({ fetch, cookies, setHeaders }) {
  setHeaders({ "cache-control": "private, no-store" });
  const lobby = await grimoireJson(fetch, cookies, "/lobby");
  const campaigns = await Promise.all(
    lobby.campaigns.map(async (campaign) => {
      const needs_character =
        campaign.role === "player" && !campaign.player_character_id;
      if (campaign.role !== "dm") return { ...campaign, needs_character };
      const [members, characters, invitations] = await Promise.all([
        grimoireJson(fetch, cookies, `/campaigns/${campaign.id}/members`),
        grimoireJson(fetch, cookies, `/campaigns/${campaign.id}/characters`),
        campaign.is_owner
          ? grimoireJson(
              fetch,
              cookies,
              `/campaigns/${campaign.id}/invitations`,
            )
          : [],
      ]);
      const assigned = new Set(
        members.map((member) => member.player_character_id),
      );
      const unassigned_characters = characters.filter(
        (character) => !assigned.has(character.id),
      );
      return {
        ...campaign,
        needs_character,
        members,
        unassigned_characters,
        invitations,
      };
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
      if (isRedirect(error)) throw error;
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
  assign: action((data, api) => {
    const campaignId = id(data, "campaign_id");
    const memberId = id(data, "member_id");
    let body;
    switch (data.get("mode")) {
      case "existing":
        body = { player_character_id: id(data, "player_character_id") };
        break;
      case "new":
        body = { new: { name: characterName(data) } };
        break;
      case "clear":
        body = { player_character_id: null };
        break;
      default:
        throw new Error("Invalid assignment mode.");
    }
    return api(
      `/campaigns/${campaignId}/members/${memberId}/character`,
      "PUT",
      body,
    );
  }),
  createCharacter: action(async (data, api) => {
    await api(`/campaigns/${id(data, "campaign_id")}/characters/self`, "POST", {
      name: characterName(data),
    });
    redirect(303, "/grimoire/sheets");
  }),
};
