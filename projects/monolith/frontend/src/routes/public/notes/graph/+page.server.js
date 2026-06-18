import { redirect } from "@sveltejs/kit";

// The standalone /notes/graph page is gone: the graph is now an in-page view of
// the /app/notes chat (toggle Chat | Graph). Permanent-redirect the old URL so
// existing links and the prior sitemap entry do not 404.
export function load() {
  throw redirect(308, "/app/notes");
}
