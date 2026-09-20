// Forward only the dedicated, signed Grimoire ID token. The backend verifies
// it against its own issuer and audience; projected email is never authority.
export function grimoireHeaders(cookies) {
  const token = cookies.get("grimoire-id-token");
  if (!token)
    throw new Error("Your session has expired. Please sign in again.");
  return { "x-grimoire-token": token };
}

export async function grimoireJson(fetch, cookies, path, options = {}) {
  const response = await fetch(`${process.env.API_BASE}/api/grimoire${path}`, {
    ...options,
    signal: AbortSignal.timeout(10_000),
    headers: { ...options.headers, ...grimoireHeaders(cookies) },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new Error(
      typeof body?.detail === "string"
        ? body.detail
        : "Grimoire could not complete that request.",
    );
  }
  return response.status === 204 ? null : response.json();
}
