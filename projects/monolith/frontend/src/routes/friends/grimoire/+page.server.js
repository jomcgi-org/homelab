import { fail, isRedirect, redirect } from "@sveltejs/kit";
import { grimoireJson } from "$lib/server/grimoire-auth.js";
import {
  JOIN_PATH,
  JoinLinkError,
  JOIN_UNAVAILABLE,
  joinMetadata,
  joinToken,
  linksEnabled,
  safeJoinMessage,
} from "$lib/server/grimoire-join-links.js";

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
  const invitation_links_enabled =
    linksEnabled() && lobby.invitation_links_enabled === true;
  const campaigns = await Promise.all(
    lobby.campaigns.map(async (campaign) => {
      const needs_character =
        campaign.role === "player" && !campaign.player_character_id;
      if (campaign.role !== "dm") return { ...campaign, needs_character };
      const [members, characters, invitations, joinLinks] = await Promise.all([
        grimoireJson(fetch, cookies, `/campaigns/${campaign.id}/members`),
        grimoireJson(fetch, cookies, `/campaigns/${campaign.id}/characters`),
        campaign.is_owner
          ? grimoireJson(
              fetch,
              cookies,
              `/campaigns/${campaign.id}/invitations`,
            )
          : [],
        campaign.is_owner && invitation_links_enabled
          ? grimoireJson(
              fetch,
              cookies,
              `/campaigns/${campaign.id}/join-links`,
            ).catch(() => null)
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
        join_links: (joinLinks || []).map(joinMetadata),
        ...(joinLinks === null ? { join_links_error: JOIN_UNAVAILABLE } : {}),
      };
    }),
  );
  return {
    ...lobby,
    campaigns,
    invitation_links_enabled,
    invitation_enrollment_enabled:
      invitation_links_enabled && lobby.invitation_enrollment_enabled === true,
    playEnabled: process.env.GRIMOIRE_PLAY_ENABLED === "true",
  };
}

function action(run, invitationAction = false) {
  return async ({ request, fetch, cookies, url }) => {
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
      const result = await run(data, api, url);
      return result?.new_link ? result : { ok: true };
    } catch (error) {
      if (isRedirect(error)) throw error;
      return fail(400, {
        error: invitationAction ? safeJoinMessage(error) : error.message,
      });
    }
  };
}

export const actions = {
  createLink: action(async (data, api, url) => {
    if (!linksEnabled()) throw new JoinLinkError(JOIN_UNAVAILABLE);
    const campaignId = id(data, "campaign_id");
    const email = data.get("email");
    if (
      typeof email !== "string" ||
      email.trim().length > 320 ||
      !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email.trim())
    ) {
      throw new JoinLinkError("Enter the player's email address.");
    }
    const enrollment = data.get("allow_enrollment");
    if (enrollment !== null && enrollment !== "on")
      throw new JoinLinkError("Invalid account invitation selection.");
    const created = await api(`/campaigns/${campaignId}/join-links`, "POST", {
      email: email.trim(),
      allow_enrollment: enrollment === "on",
    });
    return {
      ok: true,
      new_link: {
        ...joinMetadata(created),
        campaign_id: campaignId,
        url: `${url.origin}${JOIN_PATH}#${joinToken(created.token)}`,
      },
    };
  }, true),
  revokeLink: action(async (data, api) => {
    if (!linksEnabled()) throw new JoinLinkError(JOIN_UNAVAILABLE);
    await api(
      `/campaigns/${id(data, "campaign_id")}/join-links/${id(data, "join_link_id")}`,
      "DELETE",
    );
  }, true),
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
