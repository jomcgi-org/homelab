import { fail } from "@sveltejs/kit";

const API_BASE = process.env.API_BASE;
const UUID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

function authenticatedHeaders(request, supplied) {
  const headers = new Headers(supplied);
  for (const name of [
    "x-grimoire-token",
    "authorization",
    "cf-access-jwt-assertion",
    "x-auth-email",
  ]) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
  return headers;
}

async function apiJson(fetch, request, path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, {
    signal: AbortSignal.timeout(10_000),
    ...options,
    headers: authenticatedHeaders(request, options.headers),
  });
  if (!response.ok) throw new Error(await readError(response));
  return response.json();
}

async function readError(response) {
  const text = await response.text();
  try {
    const body = JSON.parse(text);
    if (typeof body?.detail === "string") return body.detail;
    if (Array.isArray(body?.detail)) {
      return body.detail
        .map((item) => item.msg)
        .filter(Boolean)
        .join("; ");
    }
  } catch {
    // Preserve the upstream text when it is not a FastAPI JSON error.
  }
  return text || `request failed (${response.status})`;
}

function sheetsPath(campaignId, characterId) {
  const campaign = encodeURIComponent(campaignId);
  const character = encodeURIComponent(characterId);
  return `/api/grimoire/campaigns/${campaign}/characters/${character}/sheets`;
}

export async function load({ fetch, request }) {
  try {
    const campaigns = await apiJson(fetch, request, "/api/grimoire/campaigns");
    const groups = await Promise.all(
      campaigns.map(async (campaign) => {
        const characters = await apiJson(
          fetch,
          request,
          `/api/grimoire/campaigns/${encodeURIComponent(campaign.id)}/characters`,
        );
        const workspaces = await Promise.all(
          characters.map((character) =>
            apiJson(fetch, request, sheetsPath(campaign.id, character.id)),
          ),
        );
        return { campaign, workspaces };
      }),
    );
    return { groups, unavailable: false };
  } catch (error) {
    return {
      groups: [],
      unavailable: true,
      message: error?.message ?? "unavailable",
    };
  }
}

function ids(data, includeVersion = false) {
  const campaignId = data.get("campaign_id");
  const characterId = data.get("character_id");
  const versionId = data.get("version_id");
  if (!UUID.test(campaignId) || !UUID.test(characterId)) return null;
  if (includeVersion && !UUID.test(versionId)) return null;
  return { campaignId, characterId, versionId };
}

function sheetFrom(data) {
  const score = (name) => Number(data.get(name));
  return {
    schema_version: 1,
    ancestry: String(data.get("ancestry") ?? ""),
    class_name: String(data.get("class_name") ?? ""),
    level: Number(data.get("level")),
    ability_scores: {
      strength: score("strength"),
      dexterity: score("dexterity"),
      constitution: score("constitution"),
      intelligence: score("intelligence"),
      wisdom: score("wisdom"),
      charisma: score("charisma"),
    },
  };
}

export const actions = {
  save: async ({ request, fetch }) => {
    const data = await request.formData();
    const parsed = ids(data);
    if (!parsed) return fail(400, { error: "invalid character" });
    const base = sheetsPath(parsed.campaignId, parsed.characterId);
    const versionId = data.get("version_id");
    const path = UUID.test(versionId)
      ? `${base}/${versionId}`
      : `${base}/drafts`;
    try {
      await apiJson(fetch, request, path, {
        method: UUID.test(versionId) ? "PATCH" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(sheetFrom(data)),
      });
      return { ok: true };
    } catch (error) {
      return fail(422, { error: error?.message ?? "could not save draft" });
    }
  },

  submit: async ({ request, fetch }) => {
    const data = await request.formData();
    const parsed = ids(data, true);
    if (!parsed) return fail(400, { error: "invalid sheet version" });
    try {
      await apiJson(
        fetch,
        request,
        `${sheetsPath(parsed.campaignId, parsed.characterId)}/${parsed.versionId}/submit`,
        { method: "POST" },
      );
      return { ok: true };
    } catch (error) {
      return fail(409, { error: error?.message ?? "could not submit draft" });
    }
  },

  decide: async ({ request, fetch }) => {
    const data = await request.formData();
    const parsed = ids(data, true);
    const decision = data.get("decision");
    if (!parsed || !new Set(["approve", "return"]).has(decision)) {
      return fail(400, { error: "invalid sheet decision" });
    }
    try {
      await apiJson(
        fetch,
        request,
        `${sheetsPath(parsed.campaignId, parsed.characterId)}/${parsed.versionId}/${decision}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            comment: String(data.get("comment") ?? "") || null,
          }),
        },
      );
      return { ok: true };
    } catch (error) {
      return fail(409, { error: error?.message ?? "could not decide sheet" });
    }
  },
};
