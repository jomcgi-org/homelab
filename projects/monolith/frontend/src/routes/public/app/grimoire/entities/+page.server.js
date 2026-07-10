// The old Entities index merged into World (routes/public/app/grimoire/world).
// Permanently redirect there. Entities' search (?q=) and type filter don't map
// onto a URL param World reads (World's search is a live typeahead, not a URL
// state), so this is a plain redirect to the World landing rather than a
// param-mapped one.
//
// Runs server-side despite the ssr=false route tree: a +page.server.js load
// always executes on the server regardless of the ssr flag (see the sibling
// chat/+page.server.js docblock).
import { redirect } from "@sveltejs/kit";

export function load() {
  throw redirect(301, "/app/grimoire/world");
}
