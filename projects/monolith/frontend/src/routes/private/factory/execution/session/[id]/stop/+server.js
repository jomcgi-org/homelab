const API_BASE = process.env.API_BASE;

function enabled() {
  return (
    String(process.env.AGENT_SESSION_STOP_CONTROL_ENABLED).toLowerCase() ===
    "true"
  );
}

export async function POST({ params, request }) {
  if (!enabled()) {
    return new Response(JSON.stringify({ error: "session Stop is disabled" }), {
      status: 404,
      headers: { "Content-Type": "application/json" },
    });
  }

  try {
    const body = await request.json();
    const email = request.headers.get("x-auth-email");
    const res = await fetch(
      `${API_BASE}/api/agents/sessions/${encodeURIComponent(params.id)}/stop`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(email ? { "X-Auth-Email": email } : {}),
        },
        body: JSON.stringify({
          turn_seq: body.turn_seq,
          dispatch_id: body.dispatch_id,
        }),
        signal: AbortSignal.timeout(40000),
      },
    );
    return new Response(res.body, {
      status: res.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch {
    return new Response(
      JSON.stringify({
        outcome: "unknown",
        reason: "relay_unavailable",
      }),
      {
        status: 502,
        headers: { "Content-Type": "application/json" },
      },
    );
  }
}
