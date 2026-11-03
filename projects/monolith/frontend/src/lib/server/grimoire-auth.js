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
  const retryGet =
    process.env.GRIMOIRE_ROLLOUT_RECOVERY_ENABLED === "true" &&
    (options.method ?? "GET").toUpperCase() === "GET";
  // Both attempts share a deadline. A terminating pod cannot double the
  // old ten-second wait and hold the next two-second session poll behind it.
  const deadline = AbortSignal.timeout(retryGet ? 1500 : 10_000);
  const signal = options.signal
    ? AbortSignal.any([options.signal, deadline])
    : deadline;
  const headers = new Headers(options.headers);
  for (const [name, value] of Object.entries(grimoireHeaders(cookies)))
    headers.set(name, value);
  const request = {
    ...options,
    signal,
    headers: Object.fromEntries(headers),
  };
  const url = `${process.env.API_BASE}/api/grimoire${path}`;
  let response;
  for (let attempt = 0; attempt < (retryGet ? 2 : 1); attempt += 1) {
    signal.throwIfAborted();
    try {
      response = await fetch(url, request);
    } catch (error) {
      // Fetch connection failures are TypeErrors. Caller aborts and deadline
      // expiry must not start another request, even if a fetch wraps the abort.
      if (
        retryGet &&
        attempt === 0 &&
        error instanceof TypeError &&
        !signal.aborted
      )
        continue;
      throw error;
    }
    if (
      retryGet &&
      attempt === 0 &&
      [502, 503, 504].includes(response.status) &&
      !signal.aborted
    ) {
      // Release the discarded response without parsing it. JSON/application
      // failures below are outside the retry loop.
      await response.body?.cancel().catch(() => {});
      continue;
    }
    break;
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
