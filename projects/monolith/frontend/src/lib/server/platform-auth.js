import { error } from "@sveltejs/kit";

export function managementEnabled() {
  return process.env.PLATFORM_AUTH_MANAGEMENT_ENABLED === "true";
}

export async function platformJson(fetch, cookies, path, options = {}) {
  if (!managementEnabled()) error(404, "Account management is unavailable.");
  const token = cookies.get("grimoire-id-token");
  if (!token) error(403, "Sign in to continue.");
  const response = await fetch(
    `${process.env.API_BASE}/api/auth/platform${path}`,
    {
      ...options,
      signal: AbortSignal.timeout(10_000),
      headers: {
        ...options.headers,
        "x-platform-token": token,
      },
    },
  );
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    error(
      response.status,
      typeof body?.detail === "string"
        ? body.detail
        : "Account request could not finish.",
    );
  }
  return response.json();
}
