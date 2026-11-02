import { invalidateTasks, readTasks } from "$lib/private/backend-cache.js";
import { loadDashboard } from "$lib/private/dashboard-load.js";

// No URL fallback: API_BASE is injected via values.yaml in prod; a localhost
// dev run sets it in the environment. A missing var fails loudly instead of
// silently serving from the wrong backend.
const API_BASE = process.env.API_BASE;

// Statuses considered "done" for the checkbox toggle. Mirrors the
// vocabulary in knowledge/store.py patch_task (done_statuses).
const DONE_STATUSES = ["done", "cancelled"];

export async function load() {
  // Every section comes from the process-wide backend cache: the page
  // answers from memory and refreshes in the background, so a stalled
  // backend never holds the page past the gateway's timeout. A cold process
  // waits a few seconds at most for its first reading.
  const [{ dashboard }, daily, weekly] = await Promise.all([
    loadDashboard(API_BASE),
    readTasks(API_BASE, "daily"),
    readTasks(API_BASE, "weekly"),
  ]);
  return {
    dashboard,
    tasksDaily: daily?.tasks ?? [],
    tasksWeekly: weekly?.tasks ?? [],
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
      // The cached lists still hold the old status: read them again next.
      invalidateTasks();
      return { status: next };
    } catch {
      return { error: true };
    }
  },
};
