import { relaySse } from "$lib/server/sse-relay.js";

/**
 * SSE proxy for the cluster chat agent. Mirrors routes/private/chat/+server.js
 * (the knowledge explorer proxy) but targets /api/chat/cluster.
 */
export async function POST({ request }) {
  return relaySse("/api/chat/cluster", request);
}
