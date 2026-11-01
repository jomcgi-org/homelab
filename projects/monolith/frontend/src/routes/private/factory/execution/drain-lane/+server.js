// Server-side proxy for the knowledge extraction queue strip.
import { relayJson } from "$lib/server/factory-relay.js";

export async function GET() {
  return relayJson("/api/agents/drain-lane", "drain lane unavailable");
}
