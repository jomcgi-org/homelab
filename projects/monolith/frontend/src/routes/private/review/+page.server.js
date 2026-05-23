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

export const actions = {
  // decide: POST to the monolith with no body — used for every endpoint
  // except notes' set-visibility (which needs a JSON body).
  decide: async ({ request, fetch }) => {
    const data = await request.formData();
    const path = data.get("path");
    if (typeof path !== "string" || !path.startsWith("/api/knowledge/")) {
      return fail(400, { error: "invalid path" });
    }
    const res = await fetch(`${API_BASE}${path}`, {
      method: "POST",
      signal: AbortSignal.timeout(10_000),
    });
    if (!res.ok) {
      return fail(res.status, { error: await res.text() });
    }
    return { ok: true };
  },

  // decideWithBody: POST with a JSON body — used for notes' set-visibility.
  decideWithBody: async ({ request, fetch }) => {
    const data = await request.formData();
    const path = data.get("path");
    const body = data.get("body");
    if (typeof path !== "string" || !path.startsWith("/api/knowledge/")) {
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
      return fail(res.status, { error: await res.text() });
    }
    return { ok: true };
  },
};
