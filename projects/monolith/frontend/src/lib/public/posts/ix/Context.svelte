<script>
  import Fig from "./Fig.svelte";
  import { weights } from "./data-fit.js";
  import {
    kvCache,
    gb,
    sliderToTokens,
    tokensToSlider,
  } from "./data-context.js";

  const VRAM = 24e9;
  const DENSE = weights.find((w) => w.key === "dense").gb * 1e9;
  let tokens = $state(32_768);
  const kv = $derived(kvCache(tokens));
  const experts = $derived(VRAM - DENSE - kv.bytes);
  const pct = (bytes) => `${(bytes / VRAM) * 100}%`;
</script>

<Fig title="KV cache and experts share the same VRAM">
  {#snippet controls()}
    <label class="ix-label" for="context-tokens">Context</label>
    <input
      id="context-tokens"
      type="range"
      min="0"
      max="1000"
      value={tokensToSlider(tokens)}
      oninput={(e) => (tokens = sliderToTokens(Number(e.currentTarget.value)))}
      aria-valuetext={`${tokens.toLocaleString("en-US")} tokens`}
    />
  {/snippet}

  <div class="readout" aria-live="polite">
    <div class="big">
      <span class="allocated">{gb(kv.bytes)} GB</span>
      <span class="unit">of KV cache</span>
    </div>
    <div class="line">{tokens.toLocaleString("en-US")} tokens of context</div>
    <div class="line">
      About {kv.records.toLocaleString("en-US")} expert records that no longer fit
      in VRAM
    </div>
  </div>

  <div class="vram" aria-hidden="true">
    <div class="bar">
      <i class="b-dense" style:width={pct(DENSE)}></i>
      <i class="b-kv" style:width={pct(kv.bytes)}></i>
      <i class="b-experts" style:width={pct(experts)}></i>
    </div>
    <div class="scale"><span>0</span><span>24 GB VRAM</span></div>
    <div class="key">
      <span class="k dense">dense weights</span>
      <span class="k kv">KV cache</span>
      <span class="k experts">left for experts · {gb(experts)} GB</span>
    </div>
  </div>
</Fig>

<style>
  .big {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    align-items: baseline;
  }
  .allocated {
    color: var(--ink);
    font: 600 1.35rem / 1.2 var(--font-code);
    font-variant-numeric: tabular-nums;
  }
  .unit,
  .line {
    color: var(--ink-2);
    font: 0.75rem / 1.6 var(--font-code);
  }
  .vram {
    margin-top: 0.9rem;
  }
  .bar {
    display: flex;
    height: 1.4rem;
    border: 1px solid var(--ink);
    background: var(--band);
    overflow: hidden;
  }
  .bar i {
    display: block;
    height: 100%;
    transition: width 160ms ease;
  }
  .b-dense {
    background: var(--ink-3);
  }
  .b-kv {
    background: var(--tone-hot);
  }
  .b-experts {
    background: var(--tone-gpu);
  }
  .scale {
    display: flex;
    justify-content: space-between;
    margin-top: 0.25rem;
    color: var(--ink-3);
    font: 0.65rem var(--font-code);
  }
  .key {
    display: flex;
    flex-wrap: wrap;
    gap: 0.3rem 1rem;
    margin-top: 0.4rem;
    color: var(--ink-2);
    font: 0.65rem var(--font-code);
  }
  .k::before {
    content: "";
    display: inline-block;
    width: 0.6rem;
    height: 0.6rem;
    margin-right: 0.35rem;
    vertical-align: -0.05rem;
  }
  .k.dense::before {
    background: var(--ink-3);
  }
  .k.kv::before {
    background: var(--tone-hot);
  }
  .k.experts::before {
    background: var(--tone-gpu);
  }
  @media (prefers-reduced-motion: reduce) {
    .bar i {
      transition: none;
    }
  }
</style>
