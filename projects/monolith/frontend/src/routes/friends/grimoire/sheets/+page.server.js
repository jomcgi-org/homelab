import {
  actions as sheetActions,
  load as sheetLoad,
} from "../../../private/grimoire/+page.server.js";
import { grimoireHeaders } from "$lib/server/grimoire-auth.js";

function withIdentity(event) {
  // Do not forward unrelated operator credentials or browser-supplied headers.
  const headers = new Headers(grimoireHeaders(event.cookies));
  const contentType = event.request.headers.get("content-type");
  if (contentType) headers.set("content-type", contentType);
  return { ...event, request: new Request(event.request, { headers }) };
}

export async function load(event) {
  event.setHeaders({ "cache-control": "private, no-store" });
  return sheetLoad(withIdentity(event));
}
export const actions = Object.fromEntries(
  Object.entries(sheetActions).map(([name, run]) => [
    name,
    (event) => run(withIdentity(event)),
  ]),
);
