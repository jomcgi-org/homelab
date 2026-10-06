<script>
  import { decodeSteps, rollingRate } from "./draft-steps.js";

  // The recorded code as it streamed, and the output rate per decode step under
  // it: a step that verified a prompt-lookup draft (copied from the input) emits
  // several tokens at once, and the rate line jumps where it lands.
  let { events, position, durationMs, complete, file } = $props();

  // svelte-ignore state_referenced_locally (A replay is keyed by its recording.)
  const steps = decodeSteps(events);
  // A 1.5 s window: long enough to show the copying and writing phases as
  // levels rather than step-to-step jitter.
  const rates = rollingRate(steps, 1500);
  // Each event's step source, to tint the code a lookup step copied.
  const sources = [];
  {
    let i = 0;
    for (const step of steps) {
      let tokens = 0;
      while (i < events.length && tokens < step.tokens) {
        tokens += events[i].tokens ?? 1;
        sources.push(step.source);
        i += 1;
      }
    }
  }
  // svelte-ignore state_referenced_locally (A replay is keyed by its recording.)
  const pieces = events.map((event, i) => ({ ...event, source: sources[i] }));
  const start = steps[0]?.at ?? 0;
  const end = Math.max(steps.at(-1)?.at ?? 1, start + 1);
  const maxTokens = Math.max(8, ...steps.map((s) => s.tokens));
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
  let current = $derived(rates.findLast((r) => r.at <= position)?.rate ?? 0);
  let pre = $state();
  let arrived = $derived(pieces.filter((piece) => piece.at <= position));
  $effect(() => {
    arrived;
    if (pre) pre.scrollTop = pre.scrollHeight;
  });
</script>

<div class="code-output">
  <div class="code-head">
    <span>{file}</span>
    <span class="rate">{Math.round(current)} <small>tok/s now</small></span>
  </div>
  <!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users can scroll the output.) -->
  <pre
    bind:this={pre}
    tabindex="0"
    role="region"
    aria-label="Streaming model output"><code
      >{#each arrived as piece}<span class:copied={piece.source === "lookup"}
          >{piece.content}</span
        >{/each}{#if !complete}<span class="stream-cursor" aria-hidden="true"
        ></span>{/if}</code
    ></pre>
  <figure class="trace">
    <svg
      viewBox={`0 0 ${W} ${H}`}
      role="img"
      aria-label={`Tokens per decode step and output rate: ${counts.lookup} steps verified prompt-lookup drafts (${lookupTokens} tokens), ${counts.draft} kept a model draft, ${counts.single} produced one token.`}
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
        >Prompt lookup, 3+ tokens a step (tinted code)</span
      >
      <span class="key draft">Model draft kept, 2</span>
      <span class="key single">1 token</span>
      <span class="key rate-key">Output rate</span>
    </figcaption>
  </figure>
</div>

<style>
  .code-output {
    min-width: 0;
    display: grid;
    gap: 0.6rem;
  }
  .code-head {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    color: var(--tone-gpu);
    font: 0.7rem var(--font-code);
  }
  .rate {
    color: var(--ink);
    font-size: 0.9rem;
  }
  .rate small {
    color: var(--ink-2);
    font-size: 0.65rem;
  }
  pre {
    margin: 0;
    height: 15rem;
    overflow: auto;
    padding: 0.75rem;
    border-left: 3px solid var(--tone-gpu);
    background: color-mix(in srgb, var(--tone-gpu) 6%, var(--sheet));
    color: var(--ink);
    font: 0.68rem/1.45 var(--font-code);
    scrollbar-width: thin;
    scrollbar-color: var(--tone-gpu) var(--line);
  }
  .copied {
    background: color-mix(in srgb, var(--tone-hot) 16%, transparent);
  }
  .stream-cursor {
    display: inline-block;
    width: 0;
    height: 1em;
    margin-left: 1px;
    border-left: 2px solid var(--tone-gpu);
    vertical-align: text-bottom;
  }
  .trace {
    margin: 0;
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
