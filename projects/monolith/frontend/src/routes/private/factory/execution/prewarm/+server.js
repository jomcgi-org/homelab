const API_BASE = process.env.API_BASE;

export async function POST({ request }) {
  try {
    const body = await request.json();
    if (body?.session_id == null) return new Response(null, { status: 204 });

    await fetch(
      `${API_BASE}/api/agents/sessions/${encodeURIComponent(body.session_id)}/prewarm`,
      {
        method: "POST",
        signal: AbortSignal.timeout(10000),
      },
    );
  } catch {
    // Prewarm must stay invisible to the composer, including proxy failures.
  }
  return new Response(null, { status: 204 });
}
