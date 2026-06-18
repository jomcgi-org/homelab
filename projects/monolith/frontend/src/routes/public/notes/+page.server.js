import { redirect } from "@sveltejs/kit";

// The notes chat + graph moved to /app/notes to sit alongside the other apps
// (ships/stars/hikes/dr-jobs). Permanent-redirect the old front door so existing
// links, the homepage CTA, and any bookmarks do not 404.
export function load() {
  throw redirect(308, "/app/notes");
}
