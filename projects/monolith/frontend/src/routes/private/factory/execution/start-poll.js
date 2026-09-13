export const START_POLL_INTERVAL_MS = 1000;
export const START_POLL_BACKOFF_AFTER = 10;
export const START_POLL_MAX_INTERVAL_MS = 8000;

const TERMINAL_KINDS = new Set([
  "session",
  "run",
  "needs_input",
  "refused",
  "error",
]);

export function initialStartPoll(taskId, composed = null) {
  return {
    taskId,
    composed,
    polls: 0,
    kind: "classifying",
    terminal: false,
    result: null,
  };
}

export function startPollDelay(polls) {
  if (polls < START_POLL_BACKOFF_AFTER) return START_POLL_INTERVAL_MS;
  const exponent = polls - START_POLL_BACKOFF_AFTER + 1;
  return Math.min(
    START_POLL_INTERVAL_MS * 2 ** exponent,
    START_POLL_MAX_INTERVAL_MS,
  );
}

export function advanceStartPoll(state, result) {
  if (!result || typeof result.kind !== "string") {
    return {
      ...state,
      kind: "error",
      terminal: true,
      result: { kind: "error", message: "Invalid task start status" },
    };
  }
  if (result.kind === "classifying") {
    return { ...state, polls: state.polls + 1, result };
  }
  if (!TERMINAL_KINDS.has(result.kind)) {
    return {
      ...state,
      kind: "error",
      terminal: true,
      result: { kind: "error", message: "Unknown task start status" },
    };
  }
  return { ...state, kind: result.kind, terminal: true, result };
}
