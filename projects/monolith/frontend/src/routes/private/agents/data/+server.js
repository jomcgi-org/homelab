// The console's poll endpoint. Sessions come from /api/agents/sessions and
// the offered model catalogue from /api/agents/models (issue #4859), both
// fetched server-side against API_BASE exactly as every other per-env read
// in this app. No bundled model list exists here: whatever /models returns
// is what the picker offers, so an empty catalogue stays visibly empty.
const API_BASE = process.env.API_BASE;

export async function GET() {
  try {
    const [sessionsRes, modelsRes] = await Promise.all([
      fetch(`${API_BASE}/api/agents/sessions`, {
        signal: AbortSignal.timeout(10000),
      }),
      fetch(`${API_BASE}/api/agents/models`, {
        signal: AbortSignal.timeout(10000),
      }),
    ]);
    if (!sessionsRes.ok || !modelsRes.ok) {
      throw new Error(`backend ${sessionsRes.status}/${modelsRes.status}`);
    }
    const sessions = await sessionsRes.json();
    const models = await modelsRes.json();
    return new Response(
      JSON.stringify({
        sessions: Array.isArray(sessions)
          ? sessions
          : (sessions.sessions ?? []),
        models: Array.isArray(models.models) ? models.models : [],
      }),
      {
        status: 200,
        headers: { "Content-Type": "application/json" },
      },
    );
  } catch {
    return new Response(JSON.stringify({ error: "agents data unavailable" }), {
      status: 502,
      headers: { "Content-Type": "application/json" },
    });
  }
}
