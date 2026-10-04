import { grimoireJson } from "./grimoire-auth.js";

export const JOIN_COOKIE = "grimoire-join-resume";
export const JOIN_PATH = "/grimoire/join";
export const JOIN_UNAVAILABLE =
  "Campaign invitation links are unavailable right now. Ask the campaign owner for an invitation.";
export class JoinLinkError extends Error {
  constructor(message, signIn = false) {
    super(message);
    this.signIn = signIn;
  }
}

// Only fixed messages owned by the app may cross the invitation boundary.
// Transport errors can contain an upstream URL, including configured secrets.
const SAFE_ERRORS = new Set([
  "Your session has expired. Please sign in again.",
  "Invalid selection.",
  "Invitation links are not enabled yet.",
  "Invitation not found.",
  "Campaign owner required.",
  "Enter a valid player's email address.",
  "This player must sign in to Grimoire first, or ask an account administrator for an enrollment invitation.",
  "Account enrollment is not enabled. Ask an administrator.",
  "This player is already a campaign member.",
  "A pending invitation already exists. Revoke it before creating a replacement.",
  "Sign in with the account this invitation was created for.",
  "This invitation is no longer available.",
  "This invitation has expired. Ask for a new link.",
  "You are already a campaign member.",
  "Membership changed. Please retry joining.",
  "This player already joined. Use Remove player instead.",
  "Account enrollment is temporarily unavailable. Please retry.",
  "Account invitation cancellation needs an administrator retry.",
]);

export function safeJoinMessage(error) {
  return error instanceof JoinLinkError || SAFE_ERRORS.has(error?.message)
    ? error.message
    : JOIN_UNAVAILABLE;
}

const TOKEN = /^[A-Za-z0-9_-]{43}$/;
const COOKIE_OPTIONS = {
  path: JOIN_PATH,
  httpOnly: true,
  secure: true,
  sameSite: "lax",
};

export function linksEnabled() {
  return process.env.GRIMOIRE_INVITATION_LINKS_ENABLED === "true";
}

export function joinToken(value) {
  if (typeof value !== "string" || !TOKEN.test(value)) {
    throw new JoinLinkError(
      "This invitation link is incomplete. Open the full link shared by the campaign owner.",
    );
  }
  return value;
}

export function saveJoinToken(cookies, token) {
  // Short resume window, with the invitation's expiry always checked by the API.
  cookies.set(JOIN_COOKIE, joinToken(token), {
    ...COOKIE_OPTIONS,
    maxAge: 3600,
  });
}

export function clearJoinToken(cookies) {
  cookies.delete(JOIN_COOKIE, COOKIE_OPTIONS);
}

export function joinMetadata(row) {
  // Explicit allowlist: neither a token nor the provider invitation can escape
  // through a list, inspect, load or error response if the API adds fields.
  return {
    id: row.id,
    campaign_id: row.campaign_id,
    campaign_name: row.campaign_name,
    invitee_email: row.invitee_email,
    expires_at: row.expires_at,
    status: row.status,
    can_enroll: row.can_enroll === true,
    ...(row.enrollment_cleanup_pending === true
      ? { enrollment_cleanup_pending: true }
      : {}),
  };
}

export async function bearerJoinJson(fetch, action, token) {
  if (!linksEnabled()) throw new JoinLinkError(JOIN_UNAVAILABLE);
  if (!["inspect", "enroll"].includes(action))
    throw new JoinLinkError("Invalid invitation action.");
  const response = await fetch(
    `${process.env.API_BASE}/api/grimoire/join-links/${action}`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ token: joinToken(token) }),
      signal: AbortSignal.timeout(10_000),
    },
  );
  if (!response.ok) {
    // Signup may have finished before the browser returned to this invitation.
    if (action === "enroll" && response.status === 409) {
      throw new JoinLinkError(
        "An account may already exist. Sign in to continue with this invitation.",
        true,
      );
    }
    // Never reflect provider responses, URLs or bearer tokens into page errors.
    if ([400, 404, 409, 410].includes(response.status)) {
      throw new JoinLinkError(
        "This invitation is invalid, expired, revoked, or already used. Ask the campaign owner for a new link.",
      );
    }
    if (response.status === 403)
      throw new JoinLinkError(
        "Account creation is not available for this invitation. Sign in with the invited account.",
      );
    throw new JoinLinkError(JOIN_UNAVAILABLE);
  }
  return response.json();
}

export function enrollmentUrl(value) {
  const url = new URL(value);
  if (
    url.origin !== "https://auth.jomcgi.dev" ||
    url.pathname !== "/if/flow/grimoire-link-enrollment/" ||
    url.username ||
    url.password ||
    url.hash ||
    [...url.searchParams.keys()].length !== 1 ||
    !/^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i.test(
      url.searchParams.get("itoken") || "",
    )
  ) {
    throw new JoinLinkError(
      "Account creation is unavailable right now. Please try again later.",
    );
  }
  return url.href;
}

export async function checkJoinSelection(fetch, token, invitationId) {
  if (
    typeof invitationId !== "string" ||
    !/^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i.test(invitationId)
  ) {
    throw new JoinLinkError(
      "Invitation changed. Reopen the original link before continuing.",
    );
  }
  const invitation = await bearerJoinJson(fetch, "inspect", token);
  if (invitation.id !== invitationId) {
    throw new JoinLinkError(
      "Invitation changed. Reopen the original link before continuing.",
    );
  }
}

export async function redeemJoin(fetch, cookies, token) {
  if (!linksEnabled()) throw new JoinLinkError(JOIN_UNAVAILABLE);
  return grimoireJson(fetch, cookies, "/join-links/redeem", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ token: joinToken(token) }),
  });
}
