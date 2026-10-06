<script>
  import { decodeSteps, rollingRate } from "./draft-steps.js";

  // Tokens per decode step, coloured by where its tokens came from, and the
  // output rate over them. Steps are inferred from token arrival groups: a
  // two-token group is most likely a kept model (MTP) draft, a group of three or
  // more a verified prompt-lookup draft (copied from the input) that may have
  // been partly accepted, and the rate line rises where they land. Exact
  // acceptance needs proposal/acceptance counters from the server.
  let { events, position, durationMs, tinted = false } = $props();

  // svelte-ignore state_referenced_locally (A replay is keyed by its recording.)
  const steps = decodeSteps(events);
  // A 1.5 s window: long enough to show the copying and writing phases as
  // levels rather than step-to-step jitter. The line starts once a whole window
  // has passed (before that it would only show the window filling).
  const WINDOW = 1500;
  const start = steps[0]?.at ?? 0;
  const rates = rollingRate(steps, WINDOW).filter(
    (r) => r.at - start >= WINDOW,
  );
  const end = Math.max(steps.at(-1)?.at ?? 1, start + 1);
  const maxTokens = Math.max(4, ...steps.map((s) => s.tokens));
  const maxRate = Math.max(1, ...rates.map((r) => r.rate));
  const W = 840;
  const H = 120;
  const BARS = 54;
  const x = (at) => ((at - start) / (end - start)) * (W - 8) + 4;
  const line = rates
    .map(
      (r) =>
        `${x(r.at).toFixed(1)},${(BARS - (r.rate / maxRate) * (BARS - 6)).toFixed(1)}`,
    )
    .join(" ");
  const counts = {
    lookup: steps.filter((s) => s.source === "lookup").length,
    draft: steps.filter((s) => s.source === "draft").length,
    single: steps.filter((s) => s.source === "single").length,
  };
  const lookupTokens = steps
    .filter((s) => s.source === "lookup")
    .reduce((n, s) => n + s.tokens, 0);
</script>

<figure class="trace">
  <svg
    viewBox={`0 0 ${W} ${H}`}
    role="img"
    aria-label={`Tokens per decode step and output rate, inferred from token arrival groups: ${counts.lookup} steps of three or more tokens, likely verified prompt-lookup drafts (${lookupTokens} tokens); ${counts.draft} two-token steps, likely a kept model draft; ${counts.single} steps with one token.`}
  >
    {#each steps as step}
      <rect
        class={step.source}
        class:pending={step.at > position}
        x={x(step.at) - 1.5}
        y={H - 4 - (step.tokens / maxTokens) * (H - BARS - 10)}
        width="3"
        height={(step.tokens / maxTokens) * (H - BARS - 10)}
      />
    {/each}
    <polyline class="rate-line" points={line} />
    {#if position > start && position < durationMs}
      <line
        class="cursor"
        x1={x(Math.min(position, end))}
        x2={x(Math.min(position, end))}
        y1="0"
        y2={H}
      />
    {/if}
    <text class="axis" x="6" y="12">{Math.round(maxRate)} tok/s</text>
  </svg>
  <figcaption>
    <span class="key lookup"
      >3+ tokens, likely prompt lookup{tinted ? " (tinted code)" : ""}</span
    >
    <span class="key draft">2 tokens, draft likely kept</span>
    <span class="key single">1 token</span>
    <span class="key rate-key">Output rate</span>
  </figcaption>
</figure>

<style>
  .trace {
    margin: 0.6rem 0 0;
  }
  svg {
    display: block;
    width: 100%;
    height: auto;
  }
  rect.lookup {
    fill: var(--tone-hot);
  }
  rect.draft {
    fill: var(--tone-gpu);
  }
  rect.single {
    fill: var(--ink);
  }
  rect.pending {
    opacity: 0.15;
  }
  .rate-line {
    fill: none;
    stroke: var(--ink);
    stroke-width: 1.5;
  }
  .cursor {
    stroke: var(--tone-gpu);
    stroke-width: 1;
  }
  .axis {
    fill: var(--ink-2);
    font: 10px var(--font-code);
  }
  figcaption {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem 1rem;
    margin-top: 0.3rem;
    color: var(--ink-2);
    font: 0.65rem var(--font-code);
  }
  .key::before {
    content: "";
    display: inline-block;
    width: 0.6rem;
    height: 0.6rem;
    margin-right: 0.35rem;
    vertical-align: -0.05rem;
  }
  .key.lookup::before {
    background: var(--tone-hot);
  }
  .key.draft::before {
    background: var(--tone-gpu);
  }
  .key.single::before {
    background: var(--ink);
  }
  .key.rate-key::before {
    height: 2px;
    vertical-align: 0.2rem;
    background: var(--ink);
  }
</style>
