<script>
  import Fig from "./Fig.svelte";
  import { weights } from "./data-fit.js";
  import { CONTEXTS, kvCache, gb } from "./data-context.js";

  const VRAM = 24e9;
  const DENSE = weights.find((w) => w.key === "dense").gb * 1e9;
  const rows = CONTEXTS.map((tokens) => {
    const kv = kvCache(tokens);
    return { tokens, kv, experts: VRAM - DENSE - kv.bytes };
  });
  const pct = (bytes) => `${(bytes / VRAM) * 100}%`;
</script>

<Fig title="KV cache and experts share the same VRAM">
  <div class="rows">
    {#each rows as r}
      <div class="row">
        <span class="tokens">{r.tokens.toLocaleString("en-US")} tokens</span>
        <span
          class="bar"
          role="img"
          aria-label={`${gb(r.kv.bytes)} GB of KV cache, ${gb(r.experts)} GB left for experts`}
        >
          <i class="b-dense" style:width={pct(DENSE)}></i>
          <i class="b-kv" style:width={pct(r.kv.bytes)}></i>
          <i class="b-experts" style:width={pct(r.experts)}></i>
        </span>
        <span class="value"
          ><b>{gb(r.kv.bytes)} GB</b> KV · {r.kv.records.toLocaleString(
            "en-US",
          )} records out</span
        >
      </div>
    {/each}
  </div>
  <div class="key" aria-hidden="true">
    <span class="k dense">dense weights</span>
    <span class="k kv">KV cache</span>
    <span class="k experts">expert records</span>
    <span class="scale">bar = 24 GB VRAM</span>
  </div>
</Fig>

<style>
  .rows {
    display: grid;
    gap: 0.6rem;
  }
  .row {
    display: grid;
    grid-template-columns: 7.5rem minmax(0, 1fr) 14.5rem;
    gap: 0.75rem;
    align-items: center;
    font: 0.7rem var(--font-code);
  }
  .tokens {
    color: var(--ink);
    font-variant-numeric: tabular-nums;
  }
  .bar {
    display: flex;
    height: 1.1rem;
    border: 1px solid var(--ink);
  }
  .bar i {
    display: block;
    height: 100%;
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
  .value {
    color: var(--ink-2);
    font-variant-numeric: tabular-nums;
  }
  .value b {
    color: var(--ink);
    font-weight: 600;
  }
  .key {
    display: flex;
    flex-wrap: wrap;
    gap: 0.25rem 1rem;
    margin-top: 0.85rem;
    color: var(--ink-2);
    font: 0.68rem var(--font-code);
  }
  .k::before {
    content: "";
    display: inline-block;
    width: 0.65rem;
    height: 0.65rem;
    margin-right: 0.4rem;
    vertical-align: -0.05rem;
  }
  .dense::before {
    background: var(--ink-3);
  }
  .kv::before {
    background: var(--tone-hot);
  }
  .experts::before {
    background: var(--tone-gpu);
  }
  .scale {
    margin-left: auto;
  }
  @media (max-width: 600px) {
    .row {
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 0.3rem 0.5rem;
    }
    .bar {
      grid-column: 1 / -1;
      grid-row: 2;
    }
  }
</style>
