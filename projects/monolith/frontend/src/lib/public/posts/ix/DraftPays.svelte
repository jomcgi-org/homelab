<script>
  import Fig from "./Fig.svelte";
  import {
    DRAFT_TOKENS,
    ONE_TOKEN_RATE,
    breakEven,
    draftRate,
    lookupKept,
  } from "./data-draft-pays.js";

  // Drawn at 1:1 pixels once the page measures it; 640 until then.
  let width = $state(640);
  const VBW = $derived(Math.max(300, Math.round(width)));
  const H = 246;
  const L = 40,
    R = 12,
    T = 44,
    B = 34;
  const YMAX = 80;
  const x = (k) => L + (k / DRAFT_TOKENS) * (VBW - L - R);
  const y = (r) => T + (1 - r / YMAX) * (H - T - B);
  const ks = Array.from({ length: DRAFT_TOKENS + 1 }, (_, i) => i);
  const band = $derived(
    [
      ...ks.map((k) => `${x(k)},${y(draftRate(k).high)}`),
      ...ks.toReversed().map((k) => `${x(k)},${y(draftRate(k).low)}`),
    ].join(" "),
  );
  // Prompt lookup on file edits keeps 92–95% of its 7-token drafts.
  const lookup = {
    low: lookupKept.low * DRAFT_TOKENS,
    high: lookupKept.high * DRAFT_TOKENS,
  };
  const r0 = (v) => Math.round(v);
</script>

<Fig title="When does checking a 7-token draft pay?">
  <div class="plot" bind:clientWidth={width}>
    <svg
      viewBox={`0 0 ${VBW} ${H}`}
      role="img"
      aria-label={`Tokens per second against draft tokens kept. A draft step beats ${r0(ONE_TOKEN_RATE)} tok/s once it keeps ${breakEven.low.toFixed(1)} to ${breakEven.high} tokens. Prompt lookup on file edits keeps ${lookup.low.toFixed(1)} to ${lookup.high.toFixed(1)}.`}
    >
      {#each [0, 20, 40, 60, 80] as v}
        <line x1={L} x2={VBW - R} y1={y(v)} y2={y(v)} class="grid" />
        <text x={L - 6} y={y(v) + 3.5} class="tick" text-anchor="end">{v}</text>
      {/each}
      <rect
        x={x(breakEven.low)}
        y={T}
        width={x(breakEven.high) - x(breakEven.low)}
        height={H - T - B}
        class="even"
      />
      <text x={x(breakEven.low)} y={T - 22} class="tick ink">break-even</text>
      <rect
        x={x(lookup.low)}
        y={T}
        width={x(lookup.high) - x(lookup.low)}
        height={H - T - B}
        class="lookup"
      />
      <text x={x(lookup.high)} y={T - 6} class="tick ink" text-anchor="end"
        >lookup, file edits</text
      >
      <polygon points={band} class="band" />
      <line
        x1={L}
        x2={VBW - R}
        y1={y(ONE_TOKEN_RATE)}
        y2={y(ONE_TOKEN_RATE)}
        class="one"
      />
      <text x={L + 4} y={y(ONE_TOKEN_RATE) - 5} class="tick ink"
        >1 token per step · {r0(ONE_TOKEN_RATE)} tok/s</text
      >
      <text
        x={x(5.5)}
        y={y(draftRate(5.5).high) - 8}
        class="tick hot"
        text-anchor="end">8-token draft step</text
      >
      {#each ks as k}
        <text x={x(k)} y={H - B + 15} class="tick" text-anchor="middle"
          >{k}</text
        >
      {/each}
      <text x={VBW - R} y={H - 3} class="tick" text-anchor="end"
        >draft tokens kept of {DRAFT_TOKENS}</text
      >
      <text x={L} y={H - 3} class="tick">tok/s</text>
    </svg>
  </div>
</Fig>

<style>
  .plot {
    min-width: 0;
  }
  svg {
    display: block;
    width: 100%;
    height: auto;
    font-family: var(--font-code);
  }
  .grid {
    stroke: var(--line);
    stroke-width: 1;
  }
  .tick {
    fill: var(--ink-2);
    font-size: 11px;
  }
  .tick.ink {
    fill: var(--ink);
  }
  .tick.hot {
    fill: var(--tone-hot);
  }
  .even {
    fill: var(--band);
  }
  .lookup {
    fill: color-mix(in srgb, var(--tone-gpu) 14%, transparent);
  }
  .band {
    fill: color-mix(in srgb, var(--tone-hot) 30%, transparent);
    stroke: var(--tone-hot);
    stroke-width: 1;
  }
  .one {
    stroke: var(--ink);
    stroke-width: 1.5;
    stroke-dasharray: 5 3;
  }
</style>
