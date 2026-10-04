import { fail, redirect } from "@sveltejs/kit";
import { grimoireJson } from "$lib/server/grimoire-auth.js";
import {
  JOIN_COOKIE,
  JOIN_UNAVAILABLE,
  bearerJoinJson,
  clearJoinToken,
  checkJoinSelection,
  joinMetadata,
  joinToken,
  linksEnabled,
  redeemJoin,
  safeJoinMessage,
} from "$lib/server/grimoire-join-links.js";

export async function load({ fetch, cookies, setHeaders }) {
  setHeaders({
    "cache-control": "private, no-store",
    // Preserve native Accept/Close form origins for SvelteKit's CSRF checks.
    "referrer-policy": "same-origin",
  });
  if (!linksEnabled()) return { error: JOIN_UNAVAILABLE };
  try {
    const token = joinToken(cookies.get(JOIN_COOKIE));
    const [lobby, invitation] = await Promise.all([
      grimoireJson(fetch, cookies, "/lobby"),
      bearerJoinJson(fetch, "inspect", token),
    ]);
    if (lobby.invitation_links_enabled !== true)
      return { error: JOIN_UNAVAILABLE };
    return {
      invitation: joinMetadata(invitation),
      email: lobby.user.email,
      matches:
        lobby.user.email.trim().toLowerCase() ===
        invitation.invitee_email.trim().toLowerCase(),
    };
  } catch (error) {
    return { error: safeJoinMessage(error) };
  }
}

export const actions = {
  accept: async ({ request, fetch, cookies }) => {
    try {
      if (!linksEnabled()) return fail(503, { error: JOIN_UNAVAILABLE });
      const data = await request.formData();
      const token = joinToken(cookies.get(JOIN_COOKIE));
      await checkJoinSelection(fetch, token, data.get("invitation_id"));
      await redeemJoin(fetch, cookies, token);
    } catch (error) {
      return fail(400, { error: safeJoinMessage(error) });
    }
    // Never use a provider redirect or an unvalidated identifier as a target.
    clearJoinToken(cookies);
    redirect(303, "/grimoire");
  },
  cancel: async ({ cookies }) => {
    clearJoinToken(cookies);
    redirect(303, "/grimoire");
  },
};
