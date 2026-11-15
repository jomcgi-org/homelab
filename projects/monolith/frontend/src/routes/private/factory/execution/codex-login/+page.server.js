import { error } from "@sveltejs/kit";

const STATES = new Set(["none", "pending", "granted", "failed"]);

export async function load({ url, fetch, setHeaders }) {
  const selections = url.searchParams.getAll("environment");
  const environment = selections[0] ?? "prod";
  if (
    selections.length > 1 ||
    (environment !== "prod" && environment !== "dev")
  ) {
    error(400, "Codex login environment must be prod or dev");
  }
  setHeaders({ "cache-control": "private, no-store" });

  const query = new URLSearchParams({ grant: "codex-cluster", environment });
  try {
    const response = await fetch(
      `/private/factory/execution/codex-login/status?${query}`,
      { signal: AbortSignal.timeout(10000), cache: "no-store" },
    );
    if (!response.ok) return { environment, state: "unavailable" };
    const body = await response.json();
    if (
      !STATES.has(body?.state) ||
      (environment === "dev" &&
        (body.environment !== "dev" || body.grant !== "codex-cluster"))
    ) {
      return { environment, state: "unavailable" };
    }
    return { environment, state: body.state };
  } catch {
    return { environment, state: "unavailable" };
  }
}
