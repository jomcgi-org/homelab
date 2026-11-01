import { relayRunAction } from "$lib/server/factory-relay.js";

export async function POST({ params, request }) {
  return relayRunAction(request, [params.id, "cancel"]);
}
