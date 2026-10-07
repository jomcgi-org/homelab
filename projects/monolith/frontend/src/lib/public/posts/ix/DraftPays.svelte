<script>
  import Fig from "./Fig.svelte";
  import {
    DRAFT_TOKENS,
    ONE_TOKEN_RATE,
    STEP_MS,
    breakEven,
    catchUp,
    draftRate,
    lookupOutcome,
    verdict,
  } from "./data-draft-pays.js";

  let kept = $state(6);
  let ring = $state("on");
  const rate = $derived(draftRate(kept));
  const call = $derived(
    { loses: "Loses.", even: "About even.", pays: "Pays." }[verdict(kept)],
  );

  // Drawn at 1:1 pixels: the viewBox follows the rendered width.
  let width = $state(360);
  const VBW = $derived(Math.max(300, Math.round(width)));
  const H = 190;
  const L = 34,
    R = 10,
    T = 12,
    B = 30;
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
  const r0 = (v) => Math.round(v);
  const pick = $derived(catchUp[ring]);
</script>

<Fig title="When does checking a 7-token draft pay?">
  {#snippet controls()}
    <label class="ix-label" for="draft-kept"
      >Kept {kept} of {DRAFT_TOKENS}</label
    >
    <input
      id="draft-kept"
      type="range"
      min="0"
      max={DRAFT_TOKENS}
      step="1"
      bind:value={kept}
      aria-valuetext={`${kept} of ${DRAFT_TOKENS} draft tokens kept`}
    />
  {/snippet}

  <div class="plot" bind:clientWidth={width}>
    <svg
      viewBox={`0 0 ${VBW} ${H}`}
      role="img"
      aria-label={`Tokens per second against draft tokens kept. Keeping ${kept}: ${r0(rate.low)} to ${r0(rate.high)} tok/s, against ${r0(ONE_TOKEN_RATE)} one token at a time.`}
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
      <polygon points={band} class="band" />
      <line
        x1={L}
        x2={VBW - R}
        y1={y(ONE_TOKEN_RATE)}
        y2={y(ONE_TOKEN_RATE)}
        class="one"
      />
      <text x={L + 4} y={y(ONE_TOKEN_RATE) - 5} class="tick ink"
        >1 token per step</text
      >
      <line x1={x(kept)} x2={x(kept)} y1={T} y2={H - B} class="cursor" />
      <line
        x1={x(kept)}
        x2={x(kept)}
        y1={y(rate.high)}
        y2={y(rate.low)}
        class="span"
      />
      {#each ks as k}
        <text x={x(k)} y={H - B + 14} class="tick" text-anchor="middle"
          >{k}</text
        >
      {/each}
      <text x={VBW - R} y={H - 3} class="tick" text-anchor="end"
        >draft tokens kept</text
      >
      <text x={L} y={H - 3} class="tick">tok/s</text>
    </svg>
  </div>

  <div class="readout" aria-live="polite">
    <span class="num">{r0(rate.low)}–{r0(rate.high)} tok/s</span>
    against {r0(ONE_TOKEN_RATE)} one token at a time. {call}
  </div>

  <div class="facts" aria-label="Measured with prompt lookup">
    <div>
      <span class="label">lookup drafts kept</span><span class="num"
        >{lookupOutcome.kept}</span
      >
    </div>
    <div>
      <span class="label">file edits</span><span class="num"
        >{lookupOutcome.edits}</span
      >
    </div>
    <div>
      <span class="label">tests</span><span class="num"
        >{lookupOutcome.tests}</span
      >
    </div>
    <div>
      <span class="label">prose</span><span class="num"
        >{lookupOutcome.prose}</span
      >
    </div>
  </div>

  <div class="ring">
    <span class="label">Model drafts after a run of lookup steps</span>
    <div class="toggle" role="group" aria-label="Draft head after lookup steps">
      <button
        type="button"
        aria-pressed={ring === "off"}
        onclick={() => (ring = "off")}>Head falls behind</button
      >
      <button
        type="button"
        aria-pressed={ring === "on"}
        onclick={() => (ring = "on")}>Head catches up</button
      >
    </div>
    <span class="ring-out" aria-live="polite"
      ><span class="num">{pick.kept}%</span> kept ·
      <span class="num">{pick.rate} tok/s</span></span
    >
  </div>

  {#snippet note()}
    Arithmetic on measured step costs: {STEP_MS.low}–{STEP_MS.high} ms for 8 new tokens,
    about 25 ms for one. Lookup and catch-up results measured warm on the 4090.
  {/snippet}
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
    font-size: 10px;
  }
  .tick.ink {
    fill: var(--ink);
  }
  .even {
    fill: var(--band);
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
  .cursor {
    stroke: var(--ink-3);
    stroke-width: 1;
  }
  .span {
    stroke: var(--tone-hot);
    stroke-width: 5;
    stroke-linecap: round;
  }
  .readout {
    margin-top: 0.5rem;
    color: var(--ink);
    font-size: 0.85rem;
    line-height: 1.45;
  }
  .num {
    color: var(--ink);
    font: 600 0.85rem var(--font-code);
    font-variant-numeric: tabular-nums;
  }
  .label {
    color: var(--ink-2);
    font: 0.65rem var(--font-code);
  }
  .facts {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 0.5rem 1rem;
    margin-top: 0.75rem;
    padding-top: 0.6rem;
    border-top: 1px solid var(--line);
  }
  .facts div {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
    min-width: 0;
  }
  .ring {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.5rem 1rem;
    margin-top: 0.75rem;
    padding-top: 0.6rem;
    border-top: 1px solid var(--line);
  }
  .ring > .label {
    flex-basis: 100%;
  }
  .toggle {
    display: flex;
    border: 1px solid var(--ink);
  }
  .toggle button {
    min-height: 2.25rem;
    padding: 0.3rem 0.65rem;
    border: 0;
    background: var(--sheet);
    color: var(--ink);
    font: 0.7rem var(--font-code);
    cursor: pointer;
  }
  .toggle button + button {
    border-left: 1px solid var(--ink);
  }
  .toggle button[aria-pressed="true"] {
    background: var(--ink);
    color: var(--sheet);
  }
  .toggle button:focus-visible {
    outline: 2px solid var(--accent-ink);
    outline-offset: 2px;
  }
  @media (max-width: 600px) {
    .facts {
      grid-template-columns: 1fr 1fr;
    }
  }
</style>
