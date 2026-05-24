import { fail } from "@sveltejs/kit";

const API_BASE = process.env.API_BASE || "http://localhost:8000";

const VALID_TABS = new Set(["gaps", "notes"]);
const VALID_MODES = new Set(["pending", "audit"]);

export async function load({ url, fetch }) {
  const rawTab = url.searchParams.get("tab") ?? "gaps";
  const rawMode = url.searchParams.get("mode") ?? "pending";
  const tab = VALID_TABS.has(rawTab) ? rawTab : "gaps";
  const mode = VALID_MODES.has(rawMode) ? rawMode : "pending";

  const endpoint =
    tab === "notes"
      ? `/api/knowledge/notes/review-queue?mode=${mode}`
      : `/api/knowledge/gaps/review-queue?mode=${mode}`;

  try {
    const res = await fetch(`${API_BASE}${endpoint}`, {
      signal: AbortSignal.timeout(10_000),
    });
    if (!res.ok) {
      return {
        tab,
        mode,
        items: [],
        error: `Failed to load: ${res.status}`,
      };
    }
    const body = await res.json();
    // Backend wraps the list in a top-level key matching the tab name.
    const items = Array.isArray(body?.[tab]) ? body[tab] : [];
    return { tab, mode, items };
  } catch (e) {
    return {
      tab,
      mode,
      items: [],
      error: `Failed to load: ${e?.message ?? "network error"}`,
    };
  }
}

// Only proxy paths under /api/knowledge/ so a forged `path` field can't
// reach arbitrary backend routes. The three action variants below all
// share this allow-list.
function isAllowedPath(path) {
  return typeof path === "string" && path.startsWith("/api/knowledge/");
}

// FastAPI errors come back as {"detail": "<message>"} JSON. Pull the
// human string out so the client error banner shows
//   "cannot verify note_id='patricia-selinger': visibility is unset"
// instead of the raw JSON envelope. Falls back to the raw text on any
// parse failure so we never lose information.
async function readError(res) {
  const text = await res.text();
  try {
    const body = JSON.parse(text);
    if (body && typeof body.detail === "string") return body.detail;
    if (body && typeof body.error === "string") return body.error;
  } catch {
    /* not JSON -- fall through */
  }
  return text;
}

export const actions = {
  // decide: POST to the monolith with no body — used for every endpoint
  // except notes' set-visibility (which needs a JSON body).
  decide: async ({ request, fetch }) => {
    const data = await request.formData();
    const path = data.get("path");
    if (!isAllowedPath(path)) {
      return fail(400, { error: "invalid path" });
    }
    const res = await fetch(`${API_BASE}${path}`, {
      method: "POST",
      signal: AbortSignal.timeout(10_000),
    });
    if (!res.ok) {
      // Surface the upstream status verbatim so the client can branch on
      // 404 (item was soft-deleted in another tab) without parsing text.
      return fail(res.status, { error: await readError(res) });
    }
    return { ok: true };
  },

  // decideWithBody: POST with a JSON body — used for notes' set-visibility.
  decideWithBody: async ({ request, fetch }) => {
    const data = await request.formData();
    const path = data.get("path");
    const body = data.get("body");
    if (!isAllowedPath(path)) {
      return fail(400, { error: "invalid path" });
    }
    if (typeof body !== "string") {
      return fail(400, { error: "missing body" });
    }
    const res = await fetch(`${API_BASE}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body,
      signal: AbortSignal.timeout(10_000),
    });
    if (!res.ok) {
      return fail(res.status, { error: await readError(res) });
    }
    return { ok: true };
  },

  // deleteAction: proxy DELETE to /api/knowledge/{gaps,notes}/{id}. The
  // backend soft-deletes (gap.deleted_at set, note moved to _trash/) and
  // returns the standard review-dict shape, but we only need ok/error
  // here — the client already advanced optimistically.
  deleteAction: async ({ request, fetch }) => {
    const data = await request.formData();
    const path = data.get("path");
    if (!isAllowedPath(path)) {
      return fail(400, { error: "invalid path" });
    }
    const res = await fetch(`${API_BASE}${path}`, {
      method: "DELETE",
      signal: AbortSignal.timeout(10_000),
    });
    if (!res.ok) {
      return fail(res.status, { error: await readError(res) });
    }
    return { ok: true };
  },

  // undeleteAction: POST to /api/knowledge/{gaps,notes}/{id}/undelete to
  // restore a soft-deleted row. No body. 404 means the row never existed
  // (or for gaps, was never deleted-undelete is idempotent per row); 409
  // means the row is live, which the toast UI silently swallows.
  undeleteAction: async ({ request, fetch }) => {
    const data = await request.formData();
    const path = data.get("path");
    if (!isAllowedPath(path)) {
      return fail(400, { error: "invalid path" });
    }
    const res = await fetch(`${API_BASE}${path}`, {
      method: "POST",
      signal: AbortSignal.timeout(10_000),
    });
    if (!res.ok) {
      return fail(res.status, { error: await readError(res) });
    }
    return { ok: true };
  },
};
