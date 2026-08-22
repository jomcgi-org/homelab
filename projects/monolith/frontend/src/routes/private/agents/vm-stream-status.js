const hasError = (event) => event.error != null && event.error !== "";

export function nextStatus(prev, event) {
  const current = {
    mode: prev?.mode ?? "connecting",
    lastUpdateAt: prev?.lastUpdateAt ?? null,
    error: prev?.error ?? null,
  };

  if (current.mode === "connecting") {
    switch (event?.type) {
      case "fallback-armed":
        return { ...current, mode: "polling" };
      case "open":
      case "frame":
      case "poll-ok":
      case "poll-fail":
      case "closed":
        break;
      default:
        return current;
    }
  }

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

export function streamAge(milliseconds) {
  const seconds = Math.max(0, Math.floor(Number(milliseconds || 0) / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  return hours < 24 ? `${hours}h` : `${Math.floor(hours / 24)}d`;
}
