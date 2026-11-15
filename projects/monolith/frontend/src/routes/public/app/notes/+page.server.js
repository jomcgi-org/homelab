import { randomUUID } from "node:crypto";
import { readPublicSnapshot } from "../../../../lib/server/public-snapshots.js";
import { graphExpiresAt } from "../../../../lib/public/notes/graph-cache.js";

// The first view contains a small projection of the same public snapshot that
// the canvas fetches later. HTML is not cacheable: the browser needs the exact
// request's monotonic start time to consume this snapshot's remaining lease.
export async function load({ setHeaders }) {
  try {
    const snapshot = await readPublicSnapshot("graph");
    const graph = JSON.parse(snapshot.body);
    const expiresAt = graphExpiresAt(new Headers(snapshot.headers));
    if (expiresAt <= Date.now()) throw new Error("graph lease expired");
    const nodes = graph.nodes ?? [];
    const titles = [...nodes]
      .sort(
        (a, b) => (b.degree ?? 0) - (a.degree ?? 0) || a.id.localeCompare(b.id),
      )
      .slice(0, 8)
      .map(({ id, title }) => ({ id, title }));
    const leaseMs = expiresAt - Date.now();
    if (leaseMs <= 0) throw new Error("graph lease expired");
    const leaseToken = randomUUID();
    setHeaders({
      "cache-control": "no-store",
      "cloudflare-cdn-cache-control": "no-store",
      "server-timing": `notes-lease;desc="${leaseToken}"`,
    });
    return {
      overview: {
        nodeCount: nodes.length,
        edgeCount: graph.edges?.length ?? 0,
        titles,
        expiresAt,
        leaseMs,
        leaseToken,
      },
    };
  } catch {
    setHeaders({
      "cache-control": "no-store",
      "cloudflare-cdn-cache-control": "no-store",
    });
    return { overview: null };
  }
}
