// Forward only the dedicated, signed Grimoire ID token. The backend verifies
// it against its own issuer and audience; projected email is never authority.
export function grimoireHeaders(cookies) {
  const token = cookies.get("grimoire-id-token");
  if (!token) {
    const error = new Error("Your session has expired. Please sign in again.");
    error.status = 401;
    throw error;
  }
  return { "x-grimoire-token": token };
}

export async function grimoireJson(fetch, cookies, path, options = {}) {
  const url = `${process.env.API_BASE}/api/grimoire${path}`;
  const request = {
    ...options,
    signal: AbortSignal.timeout(10_000),
    headers: { ...options.headers, ...grimoireHeaders(cookies) },
  };
  let response;
  try {
    response = await fetch(url, request);
  } catch (failure) {
    // A pooled connection can close between selection and the read. Retry
    // only that transport failure, once, within the original deadline.
    // Writes may already have committed and must never be replayed here.
    if (
      (options.method || "GET").toUpperCase() !== "GET" ||
      failure?.cause?.code !== "UND_ERR_SOCKET" ||
      request.signal.aborted
    )
      throw failure;
    response = await fetch(url, request);
  }
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const error = new Error(
      typeof body?.detail === "string"
        ? body.detail
        : "Grimoire could not complete that request.",
    );
    error.status = response.status;
    throw error;
  }
  return response.status === 204 ? null : response.json();
}
