function searchParamsFrom(value) {
  if (value instanceof URL) return new URLSearchParams(value.search);
  if (value instanceof URLSearchParams) return new URLSearchParams(value);
  return new URLSearchParams(value ?? "");
}

export function parseUrlState(value) {
  const params = searchParamsFrom(value);
  return {
    runId: params.get("run"),
    sessionId: params.get("session"),
  };
}

export function selectRun(value, runId) {
  const params = searchParamsFrom(value);
  params.delete("session");
  if (runId == null) params.delete("run");
  else params.set("run", String(runId));
  return params.toString();
}

export function selectSession(value, sessionId) {
  const params = searchParamsFrom(value);
  if (sessionId == null) params.delete("session");
  else params.set("session", String(sessionId));
  return params.toString();
}

export function clearSelection(value) {
  const params = searchParamsFrom(value);
  params.delete("run");
  params.delete("session");
  return params.toString();
}

export function backToRun(value) {
  const params = searchParamsFrom(value);
  params.delete("session");
  return params.toString();
}

/**
 * Joins a search string onto the path the browser is already on.
 *
 * The caller must pass the live pathname rather than a literal. `/private` is
 * only the internal route prefix that src/hooks.js reroutes onto: the private
 * tier reaches this page at `/agents`, so hardcoding `/private/agents` puts an
 * address in the bar that nobody types or shares.
 */
export function withSearch(pathname, search) {
  return search ? `${pathname}?${search}` : pathname;
}
