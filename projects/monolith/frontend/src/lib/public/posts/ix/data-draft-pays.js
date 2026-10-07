// Prompt lookup drafts 7 tokens; verifying them is one 8-token step
// (ARCHITECTURE.md, "Why prompt lookup"): 110 to 125 ms when its tokens are
// new, against about 25 ms for a one-token step. The step yields the kept
// draft tokens plus the model's own next token.
export const DRAFT_TOKENS = 7;
export const STEP_MS = { low: 110, high: 125 };
export const ONE_TOKEN_MS = 25;
export const ONE_TOKEN_RATE = 1000 / ONE_TOKEN_MS;

// Tokens per second of one draft step that keeps `kept` tokens (arithmetic).
export function draftRate(kept) {
  return {
    low: ((kept + 1) * 1000) / STEP_MS.high,
    high: ((kept + 1) * 1000) / STEP_MS.low,
  };
}

// "loses", "even" or "pays" against one token per 25 ms step.
export function verdict(kept) {
  const r = draftRate(kept);
  if (r.high < ONE_TOKEN_RATE) return "loses";
  if (r.low > ONE_TOKEN_RATE) return "pays";
  return "even";
}

// Smallest and largest kept counts at which the draft step matches one token
// per step, as a fractional range (110 ms and 125 ms steps).
export const breakEven = {
  low: STEP_MS.low / ONE_TOKEN_MS - 1,
  high: STEP_MS.high / ONE_TOKEN_MS - 1,
};

// Measured (same section, max-perf config, warm medians).
export const lookupOutcome = {
  kept: "92–95%",
  perStep: "about 7",
  edits: "+22 to +40%",
  tests: "level",
  prose: "−2 to −9%",
};

// MTP catch-up across lookup steps ("Why keep residuals across lookup steps"):
// the blog demo, warm, medians of 12 and 10 requests.
export const catchUp = {
  off: { kept: 76, rate: 43.4 },
  on: { kept: 83, rate: 45.6 },
};
