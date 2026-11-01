// Recovery when the friends sign-in lapses mid-session. The gateway renews
// tokens with a refresh token; when it cannot, a fetch gets a 401 (or, from a
// browser without Sec-Fetch-Mode, a redirect to authentik) and the BFF answers
// 401 when the backend rejects the token. A full page load is the only way
// back through the login, so the page keeps the composer in this tab's
// sessionStorage and reloads, at most once a minute.

const RELOAD_KEY = "grimoire-sign-in-reload";
const RELOAD_GAP_MS = 60_000;
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

// Returns whether the composer will survive a reload.
export function keepDraft(campaignId, composer) {
  try {
    const store = storage();
    if (!composer.text.trim()) store?.removeItem(draftKey(campaignId));
    else store.setItem(draftKey(campaignId), JSON.stringify(composer));
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

// Reads and clears the kept composer. A restored choice never widens the
// audience: a recipient the page no longer offers falls back to the DM.
export function takeDraft(campaignId, { dm, characters, npcs = [] }) {
  let saved;
  try {
    const store = storage();
    saved = JSON.parse(store?.getItem(draftKey(campaignId)) ?? "null");
    store?.removeItem(draftKey(campaignId));
  } catch {
    return null;
  }
  if (typeof saved?.text !== "string" || !saved.text.trim()) return null;
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
