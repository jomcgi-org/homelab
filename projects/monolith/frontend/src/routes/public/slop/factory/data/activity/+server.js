import { error } from "@sveltejs/kit";
import { readPublicSnapshot } from "../../../../../../lib/server/public-snapshots.js";

export async function GET({ setHeaders }) {
  try {
    const snapshot = await readPublicSnapshot("activity");
    setHeaders(snapshot.headers);
    return new Response(snapshot.body, {
      headers: { "content-type": "application/json" },
    });
  } catch {
    setHeaders({
      "cache-control": "no-store",
      "cloudflare-cdn-cache-control": "no-store",
    });
    throw error(503, "activity snapshot unavailable");
  }
}
