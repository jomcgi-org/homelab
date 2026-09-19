const API_BASE = process.env.API_BASE;
const TIMEOUT_MS = 15000;

export async function POST({ params, request }) {
  const itemId = parseInt(params.id, 10);
  if (isNaN(itemId)) {
    return new Response(JSON.stringify({ error: "invalid item id" }), {
      status: 400,
      headers: { "Content-Type": "application/json" },
    });
  }

  const body = await request.json();
  const xAuthEmail = request.headers.get("x-auth-email");

  if (!xAuthEmail) {
    return new Response(JSON.stringify({ error: "missing X-Auth-Email" }), {
      status: 403,
      headers: { "Content-Type": "application/json" },
    });
  }

  try {
    const response = await fetch(
      `${API_BASE}/api/agents/factory/work-items/${itemId}/edges`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Auth-Email": xAuthEmail,
        },
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(TIMEOUT_MS),
      },
    );

    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      return new Response(JSON.stringify(error), {
        status: response.status,
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
