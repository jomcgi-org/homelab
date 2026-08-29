// Kick the lane: reuses the existing internal trigger endpoint, which also
// reaps stale and version-stranded cycles before enqueueing. That reap is
// the unwedge half of the cancel-then-kick recovery, so the console gets it
// by proxying rather than by growing a second trigger path.
const API_BASE = process.env.API_BASE;

export async function POST() {
  try {
    const res = await fetch(`${API_BASE}/internal/agent/drain`, {
      method: "POST",
      signal: AbortSignal.timeout(10000),
    });
    return new Response(res.body, {
      status: res.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch {
    return new Response(JSON.stringify({ error: "drain kick failed" }), {
      status: 502,
      headers: { "Content-Type": "application/json" },
    });
  }
}
