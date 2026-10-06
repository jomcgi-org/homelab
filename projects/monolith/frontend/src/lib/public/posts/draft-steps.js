// Decode steps recovered from a recorded token stream.
//
// oom-inference sends every token a decode step produces (the drafts the model
// kept, then its own next token) back to back once the step's compute is done,
// and the next step takes tens of milliseconds. Arrivals closer together than
// `gapMs` are one step. With one model-drafted token per step (the recorded
// configuration), a step that yields three or more tokens can only have been
// verifying a prompt-lookup draft.

/** Groups `events` ({at, content, tokens?}) into steps: {at, tokens, source}.
 * A text chunk can carry several tokens; `tokens` (attributed from the recorded
 * text with the model's tokenizer) counts them, else each event is one. */
export function decodeSteps(events, gapMs = 4) {
  const steps = [];
  let previous = -Infinity;
  for (const event of events) {
    const step = steps.at(-1);
    const n = event.tokens ?? 1;
    if (step && event.at - previous <= gapMs) step.tokens += n;
    else steps.push({ at: event.at, tokens: n });
    previous = event.at;
  }
  for (const step of steps)
    step.source =
      step.tokens >= 3 ? "lookup" : step.tokens === 2 ? "draft" : "single";
  return steps;
}

/** Output tokens per second over the `windowMs` ending at each step. */
export function rollingRate(steps, windowMs = 600) {
  return steps.map((step, i) => {
    let tokens = 0;
    let j = i;
    while (j >= 0 && step.at - steps[j].at < windowMs) {
      tokens += steps[j].tokens;
      j -= 1;
    }
    const span = j >= 0 ? step.at - steps[j].at : windowMs;
    return { at: step.at, rate: (tokens * 1000) / Math.max(span, 1) };
  });
}
