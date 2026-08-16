function apiBase() {
  const value = process.env.API_BASE;
  if (!value) {
    throw new Error("API_BASE is required for the moving planner");
  }
  return value.replace(/\/$/, "");
}

export async function load({ fetch, request, url }) {
  const scope = url.searchParams.get("scope") ?? "mine";
  const email = request.headers.get("x-auth-email");
  const base = apiBase();

  let response;
  try {
    response = await fetch(
      `${base}/api/moving/state?scope=${encodeURIComponent(scope)}`,
      {
        headers: email ? { "X-Auth-Email": email } : {},
        signal: AbortSignal.timeout(10_000),
      },
    );
  } catch {
    return { status: "unavailable", scope, state: null };
  }

  if (response.status === 403) {
    return { status: "forbidden", scope, state: null };
  }
  if (!response.ok) {
    return { status: "unavailable", scope, state: null };
  }

  try {
    return { status: "ready", scope, state: await response.json() };
  } catch {
    return { status: "unavailable", scope, state: null };
  }
}
