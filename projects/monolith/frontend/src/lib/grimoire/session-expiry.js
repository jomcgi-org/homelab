// Recovery when the friends sign-in lapses mid-session. The gateway renews
// tokens with a refresh token; when it cannot, a fetch gets a 401 (or, from a
// browser without Sec-Fetch-Mode, a redirect to authentik) and the BFF answers
// 401 when the backend rejects the token. A full page load is the only way
// back through the login, so once a second signal confirms the expiry the
// page keeps the composer in this tab's sessionStorage and reloads, at most
// once a minute.

const RELOAD_KEY = "grimoire-sign-in-reload";
const RELOAD_GAP_MS = 60_000;
const CONFIRM_GAP_MS = 2_000;
const draftKey = (campaignId) => `grimoire-draft:${campaignId}`;

function storage() {
  try {
    return globalThis.sessionStorage ?? null;
  } catch {
    return null;
  }
}

// Call before reading the body: an opaque redirect has none.
export function signInExpired(response) {
  return response.type === "opaqueredirect" || response.status === 401;
}

// A lone signal can be transient: the gateway may forward a token up to a
// second past its expiry, which the backend rejects, and one failed renewal
// (authentik restarting) heals on the next request. Expiry counts only when a
// later signal, at least two seconds after the first, confirms it.
export function expiryCheck() {
  let first = null;
  return {
    // Counts a signal; returns whether it confirms the expiry.
    signal(now = Date.now()) {
      if (first === null || now < first) first = now;
      return now - first >= CONFIRM_GAP_MS;
    },
    // Any other answer shows the sign-in still works.
    reset() {
      first = null;
    },
  };
}

// Returns whether the composer will survive a reload. The viewer (user id and
// role) goes with it, so another account in this tab never gets it back.
export function keepDraft(campaignId, viewer, composer) {
  try {
    const store = storage();
    if (!composer.text.trim()) store?.removeItem(draftKey(campaignId));
    else
      store.setItem(
        draftKey(campaignId),
        JSON.stringify({
          ...composer,
          viewer: { id: viewer.id, role: viewer.role },
        }),
      );
    return true;
  } catch {
    return !composer.text.trim();
  }
}

// The sign-in came back without a reload, so the textarea still holds the
// draft and a later reload must not bring back this copy.
export function dropDraft(campaignId) {
  try {
    storage()?.removeItem(draftKey(campaignId));
  } catch {
    /* Nothing was kept. */
  }
}

// Reads and clears the kept composer. A draft another viewer kept is
// dropped. A restored choice never widens the audience: a recipient the page
// no longer offers falls back to the DM.
export function takeDraft(campaignId, viewer, { dm, characters, npcs = [] }) {
  let saved;
  try {
    const store = storage();
    saved = JSON.parse(store?.getItem(draftKey(campaignId)) ?? "null");
    store?.removeItem(draftKey(campaignId));
  } catch {
    return null;
  }
  if (
    typeof viewer?.id !== "string" ||
    saved?.viewer?.id !== viewer.id ||
    saved.viewer.role !== viewer.role
  )
    return null;
  if (typeof saved.text !== "string" || !saved.text.trim()) return null;
  const audiences = ["table", "dm"];
  if (dm) audiences.push(...characters.map((row) => `pc:${row.id}`));
  const audience = audiences.includes(saved.audience) ? saved.audience : "dm";
  const reply = saved.replyTo;
  const speakers = ["narrator", "custom", ...npcs.map((npc) => npc.id)];
  return {
    text: saved.text.slice(0, 8000),
    audience,
    replyTo:
      audience.startsWith("pc:") &&
      typeof reply?.id === "string" &&
      typeof reply?.name === "string"
        ? { id: reply.id, name: reply.name }
        : null,
    resolved: saved.resolved !== false,
    speakerKey: speakers.includes(saved.speakerKey)
      ? saved.speakerKey
      : "narrator",
    speakerLabel: String(saved.speakerLabel ?? "").slice(0, 64),
  };
}

// Claims this tab's one reload a minute. Without storage there is no guard,
// so the page never reloads on its own.
export function claimReload(now = Date.now()) {
  try {
    const store = storage();
    const last = Number(store.getItem(RELOAD_KEY));
    if (now >= last && now - last < RELOAD_GAP_MS) return false;
    store.setItem(RELOAD_KEY, String(now));
    return true;
  } catch {
    return false;
  }
}
