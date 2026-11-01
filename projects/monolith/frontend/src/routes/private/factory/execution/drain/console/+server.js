// Server-side proxy for the drain console read.
import { relayJson } from "$lib/server/factory-relay.js";

export async function GET() {
  return relayJson("/api/agents/drain/console", "drain console unavailable");
}
