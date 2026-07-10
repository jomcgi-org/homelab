// The old Explore page merged into World (routes/public/app/grimoire/world).
// Permanently redirect, mapping the legacy params: Explore's selected entity
// (?focus=) becomes World's focus (?e=), and scope/lens carry over unchanged
// (World reads the same param names). A 301 keeps shared/bookmarked Explore
// links working and lets search engines fold the old URL into the new one.
//
// This runs server-side even though the /app/grimoire tree is ssr=false: a
// +page.server.js load always executes on the server regardless of the ssr
// flag (see the sibling chat/+page.server.js docblock).
import { redirect } from "@sveltejs/kit";

export function load({ url }) {
  const next = new URL("/app/grimoire/world", url);
  const focus = url.searchParams.get("focus");
  if (focus) next.searchParams.set("e", focus);
  const scope = url.searchParams.get("scope");
  if (scope && scope !== "everything") next.searchParams.set("scope", scope);
  const lens = url.searchParams.get("lens");
  if (lens && lens !== "world") next.searchParams.set("lens", lens);
  throw redirect(301, next.pathname + next.search);
}
