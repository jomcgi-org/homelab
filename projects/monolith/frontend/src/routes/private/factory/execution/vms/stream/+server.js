// No localhost fallback, matching the sibling vms/+server.js: an unset
// API_BASE must fail loudly rather than quietly stream from nowhere.
const API_BASE = process.env.API_BASE;

export async function GET() {
  try {
    const res = await fetch(`${API_BASE}/api/agents/vms/stream`);
    return new Response(res.body, {
      status: res.status,
      headers: {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        Connection: "keep-alive",
      },
    });
  } catch {
    return new Response("vm state stream unavailable", { status: 502 });
  }
}
