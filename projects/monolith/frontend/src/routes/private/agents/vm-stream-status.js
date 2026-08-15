const hasError = (event) => event.error != null && event.error !== "";

export function nextStatus(prev, event) {
  const current = {
    mode: prev?.mode ?? "stalled",
    lastUpdateAt: prev?.lastUpdateAt ?? null,
    error: prev?.error ?? null,
  };

  switch (event?.type) {
    case "open":
      return { ...current, mode: "streaming", error: null };
    case "frame":
      if (hasError(event)) {
        return { ...current, mode: "stalled", error: event.error };
      }
      return {
        mode: "streaming",
        lastUpdateAt: event.at,
        error: null,
      };
    case "poll-ok":
      if (hasError(event)) {
        return { ...current, mode: "stalled", error: event.error };
      }
      return {
        mode: "polling",
        lastUpdateAt: event.at,
        error: null,
      };
    case "poll-fail":
      return {
        ...current,
        mode: "stalled",
        error: hasError(event) ? event.error : current.error,
      };
    case "closed":
      return current.mode === "polling"
        ? current
        : { ...current, mode: "stalled" };
    case "fallback-armed":
      return { ...current, mode: current.error ? "stalled" : "polling" };
    default:
      return current;
  }
}

export function formatAge(milliseconds) {
  const elapsed = Math.max(0, Number(milliseconds) || 0);
  if (elapsed < 60_000) return "just now";
  if (elapsed < 3_600_000) return `${Math.floor(elapsed / 60_000)}m ago`;
  if (elapsed < 86_400_000) return `${Math.floor(elapsed / 3_600_000)}h ago`;
  return `${Math.floor(elapsed / 86_400_000)}d ago`;
}
