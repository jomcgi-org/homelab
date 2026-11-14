import { readPublicSnapshot } from "../../../../lib/server/public-snapshots.js";
import { graphExpiresAt } from "../../../../lib/public/notes/graph-cache.js";

// The first view contains a small projection of the same public snapshot that
// the canvas fetches later. Its absolute deadline survives HTML caching and
// hydration; the page must never grant the titles a new lease in the browser.
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
    const headers = { ...snapshot.headers };
    // A renewed lease changes the HTML's absolute deadline even when the
    // graph is unchanged. Let Kit hash that HTML rather than revalidating an
    // old page against the graph-only validator and retaining its old deadline.
    delete headers.etag;
    delete headers["last-modified"];
    setHeaders(headers);
    return {
      overview: {
        nodeCount: nodes.length,
        edgeCount: graph.edges?.length ?? 0,
        titles,
        expiresAt,
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
