const API_BASE = process.env.API_BASE;
const TIMEOUT_MS = 15000;

export async function load({ params }) {
  const itemId = parseInt(params.id, 10);
  if (isNaN(itemId)) {
    return { itemId: params.id, missing: true };
  }

  try {
    const response = await fetch(
      `${API_BASE}/api/agents/factory/work-items/${itemId}`,
      {
        signal: AbortSignal.timeout(TIMEOUT_MS),
      },
    );

    if (response.status === 404) {
      return { itemId: params.id, missing: true };
    }

    if (!response.ok) {
      return { itemId: params.id, error: true };
    }

    const document = await response.json();
    return { itemId: params.id, document };
  } catch (err) {
    return { itemId: params.id, error: true };
  }
}
