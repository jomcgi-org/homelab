import { error, fail, isHttpError } from "@sveltejs/kit";
import { managementEnabled, platformJson } from "$lib/server/platform-auth.js";

export async function load({ fetch, cookies, setHeaders, url }) {
  if (!managementEnabled()) error(404, "Account management is unavailable.");
  setHeaders({
    "cache-control": "private, no-store",
    "referrer-policy": "no-referrer",
  });
  try {
    const [users, invitations, permissions] = await Promise.all([
      platformJson(fetch, cookies, "/management/users"),
      platformJson(fetch, cookies, "/management/invitations"),
      platformJson(fetch, cookies, "/management/permissions"),
    ]);
    const selected = url.searchParams.get("invitation_id");
    if (
      selected &&
      !invitations.items.some((invitation) => invitation.id === selected)
    ) {
      const invitation = await platformJson(
        fetch,
        cookies,
        `/management/invitation?invitation_id=${encodeURIComponent(selected)}`,
      );
      invitations.items.unshift(invitation);
    }
    return { users, invitations, permissions };
  } catch (problem) {
    if (isHttpError(problem) && problem.status === 403) {
      // Bootstrap imports only the signed human operator. The API rechecks it.
      return { bootstrapRequired: true };
    }
    throw problem;
  }
}

export const actions = {
  default: async ({ request, fetch, cookies, url, setHeaders }) => {
    setHeaders({
      "cache-control": "private, no-store",
      "referrer-policy": "no-referrer",
    });
    const data = await request.formData();
    const action = data.get("action");
    const reason = String(data.get("reason") || "");
    const request_id = String(data.get("request_id") || "");
    const argumentsByAction = {
      bootstrap: data.get("user_id")
        ? { user_id: String(data.get("user_id")) }
        : {},
      issue: {
        recipient_label: String(data.get("recipient_label") || ""),
        expires_in_days: 7,
      },
      revoke_invitation: {
        invitation_id: String(data.get("invitation_id") || ""),
      },
      set_active: {
        user_id: String(data.get("user_id") || ""),
        active: data.get("active") === "true",
      },
      grant: {
        user_id: String(data.get("user_id") || ""),
        permission: String(data.get("permission") || ""),
      },
      revoke_grant: {
        user_id: String(data.get("user_id") || ""),
        permission: String(data.get("permission") || ""),
      },
    };
    try {
      if (action === "deliver" || action === "reissue") {
        const id = String(data.get("invitation_id") || "");
        if (!/^[0-9a-f-]{36}$/i.test(id))
          return fail(400, { error: "Invalid invitation." });
        const delivered = await platformJson(
          fetch,
          cookies,
          `/invitations/${id}/deliver`,
          {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify({
              request_id,
              reason,
              reissue: action === "reissue",
            }),
          },
        );
        return { link: `${url.origin}/register#${delivered.token}` };
      }
      if (!Object.hasOwn(argumentsByAction, action))
        return fail(400, { error: "Invalid account action." });
      await platformJson(fetch, cookies, "/commands", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          action,
          request_id,
          reason,
          arguments: argumentsByAction[action],
        }),
      });
      return { ok: true };
    } catch (problem) {
      if (isHttpError(problem))
        return fail(problem.status, { error: problem.body.message });
      return fail(503, {
        error: "Account request could not finish. Try again.",
      });
    }
  },
};
