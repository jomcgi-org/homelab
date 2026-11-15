import { relaySse } from "$lib/server/sse-relay.js";

const API_BASE = process.env.API_BASE || "http://localhost:8000";

export async function POST({ request }) {
  return relaySse("/api/chat/explore", request);
}

export async function GET({ url }) {
  const noteId = url.searchParams.get("note_id");
  if (!noteId) return new Response("missing note_id", { status: 400 });

  const res = await fetch(
    `${API_BASE}/api/knowledge/notes/${encodeURIComponent(noteId)}`,
    { signal: AbortSignal.timeout(10000) },
  );
  return new Response(res.body, {
    status: res.status,
    headers: { "Content-Type": "application/json" },
  });
}
