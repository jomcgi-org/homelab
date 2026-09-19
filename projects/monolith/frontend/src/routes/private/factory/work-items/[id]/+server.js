const API_BASE = process.env.API_BASE;
const TIMEOUT_MS = 15000;

export async function GET({ params }) {
  const itemId = parseInt(params.id, 10);
  if (isNaN(itemId)) {
    return new Response(JSON.stringify({ error: "invalid item id" }), {
      status: 400,
      headers: { "Content-Type": "application/json" },
    });
  }

  try {
    const response = await fetch(
      `${API_BASE}/api/agents/factory/work-items/${itemId}`,
      {
        signal: AbortSignal.timeout(TIMEOUT_MS),
      },
    );

    if (!response.ok) {
      return new Response(JSON.stringify({ error: "unavailable" }), {
        status: 502,
        headers: { "Content-Type": "application/json" },
      });
    }

    const document = await response.json();
    return new Response(JSON.stringify(document), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  } catch (err) {
    return new Response(JSON.stringify({ error: "unavailable" }), {
      status: 502,
      headers: { "Content-Type": "application/json" },
    });
  }
}
