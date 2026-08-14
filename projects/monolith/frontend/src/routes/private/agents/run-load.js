// Retry strategy for loading runs. Exported for testing.
export const RUNS_LOAD_MAX_ATTEMPTS = 3;
export const RUNS_LOAD_BACKOFF_MS = 200;

// Compute the retry backoff time given the current attempt number.
// Attempts are 0-indexed, so attempt 0 waits 0ms, attempt 1 waits 200ms, etc.
export function retryBackoffMs(attempt) {
  if (attempt <= 0) return 0;
  return RUNS_LOAD_BACKOFF_MS * attempt;
}

// Decide the engine tier based on whether this is an initial load with retries exhausted.
export function degradeToTier(hasSnapshot) {
  return hasSnapshot ? { engine_tier: "stale" } : { engine_tier: "absent" };
}
