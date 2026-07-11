// No URL fallback: API_BASE is injected via values.yaml in prod; a localhost
// dev run sets it in the environment. A missing var fails loudly instead of
// silently serving from the wrong backend.
const API_BASE = process.env.API_BASE;

// Statuses considered "done" for the checkbox toggle. Mirrors the
// vocabulary in knowledge/store.py patch_task (done_statuses).
const DONE_STATUSES = ["done", "cancelled"];

export async function load({ fetch }) {
  // Fail-soft parallel fetches: a dead backend section degrades to
  // null/[] so SSR always renders.
  const [dashboard, tasksDaily, tasksWeekly] = await Promise.all([
    fetch(`${API_BASE}/api/home/dashboard`, {
      signal: AbortSignal.timeout(15000),
    })
      .then((res) => (res.ok ? res.json() : null))
      .catch(() => null),
    fetch(`${API_BASE}/api/knowledge/tasks/daily`, {
      signal: AbortSignal.timeout(10000),
    })
      .then((res) => (res.ok ? res.json() : { tasks: [] }))
      .catch(() => ({ tasks: [] })),
    fetch(`${API_BASE}/api/knowledge/tasks/weekly`, {
      signal: AbortSignal.timeout(10000),
    })
      .then((res) => (res.ok ? res.json() : { tasks: [] }))
      .catch(() => ({ tasks: [] })),
  ]);

  return {
    dashboard,
    tasksDaily: tasksDaily?.tasks ?? [],
    tasksWeekly: tasksWeekly?.tasks ?? [],
  };
}

export const actions = {
  toggleTask: async ({ request, fetch }) => {
    const data = await request.formData();
    const noteId = data.get("note_id");
    const current = data.get("status") || "";
    if (!noteId) return { error: true };
    // Real status vocabulary: todo / in-progress / done / cancelled /
    // someday. Toggling a done-ish task reopens it as todo; toggling
    // anything else completes it.
    const next = DONE_STATUSES.includes(current) ? "todo" : "done";
    try {
      const res = await fetch(
        `${API_BASE}/api/knowledge/tasks/${encodeURIComponent(noteId)}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ status: next }),
          signal: AbortSignal.timeout(10000),
        },
      );
      if (!res.ok) return { error: true };
      return { status: next };
    } catch {
      return { error: true };
    }
  },
  capture: async ({ request, fetch }) => {
    const data = await request.formData();
    const content = data.get("content");
    const res = await fetch(`${API_BASE}/api/notes`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
      signal: AbortSignal.timeout(10000),
    });
    if (!res.ok) return { error: true };
  },
  ingest: async ({ request, fetch }) => {
    const data = await request.formData();
    const url = data.get("url");
    const sourceType = data.get("source_type");
    if (!url) return { error: true };
    const res = await fetch(`${API_BASE}/api/knowledge/ingest`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, source_type: sourceType }),
      signal: AbortSignal.timeout(10000),
    });
    if (!res.ok) return { error: true };
  },
  search: async ({ request, fetch }) => {
    const data = await request.formData();
    const q = data.get("q");
    const type = data.get("type");
    if (!q) return { results: [] };
    const params = new URLSearchParams({ q });
    if (type && type !== "all") params.set("type", type);
    try {
      const res = await fetch(`${API_BASE}/api/knowledge/search?${params}`, {
        signal: AbortSignal.timeout(10000),
      });
      if (res.ok) {
        const json = await res.json();
        return { results: json.results };
      }
      if (res.status === 503)
        return { results: [], error: "embedding unavailable" };
      return { results: [], error: `search failed (${res.status})` };
    } catch {
      return { results: [], error: "search unavailable" };
    }
  },
  preview: async ({ request, fetch }) => {
    const data = await request.formData();
    const noteId = data.get("note_id");
    if (!noteId) return { note: null };
    try {
      const res = await fetch(
        `${API_BASE}/api/knowledge/notes/${encodeURIComponent(noteId)}`,
        { signal: AbortSignal.timeout(10000) },
      );
      if (res.ok) return { note: await res.json() };
      return { note: null };
    } catch {
      return { note: null };
    }
  },
};
