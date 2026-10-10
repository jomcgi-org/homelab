<script>
  import { onMount } from "svelte";
  import Fig from "./Fig.svelte";
  import { expertPaths } from "./data.js";

  let selected = $state("copy");
  let motion = $state(false);
  onMount(() => {
    motion = !matchMedia("(prefers-reduced-motion: reduce)").matches;
  });
  const path = $derived(expertPaths.find((p) => p.key === selected));
  const max = Math.max(...expertPaths.map((p) => p.micros));

  // Routes through the drawing below, one per choice.
  const routes = {
    vram: "M60,62 H420",
    copy: "M105,166 V62",
    cpu: "M220,158 H330 Q370,158 370,138 V62",
    nvme: "M105,266 V62",
  };
  const lit = (...keys) => keys.includes(selected);
  // Without motion the dot rests where its route starts.
  const start = $derived(routes[selected].match(/M(\d+),(\d+)/).slice(1));
</script>

<Fig title="Bandwidth constraints for expert tiers">
  {#snippet controls()}
    <div class="seg" role="group" aria-label="Where the expert is">
      {#each expertPaths as p}
        <button
          type="button"
          aria-pressed={selected === p.key}
          onclick={() => (selected = p.key)}>{p.label}</button
        >
      {/each}
    </div>
  {/snippet}

  <div class="layout">
    <svg
      viewBox="0 0 480 300"
      role="img"
      aria-label={`${path.label}: ${path.cost}; moves ${path.moves}.`}
    >
      <g class="tier gpu" class:on={true}>
        <rect x="20" y="16" width="440" height="70" />
        <text x="34" y="40" class="title">VRAM · 24 GB</text>
        <text x="34" y="64">dense weights · state · hot experts</text>
      </g>
      <g class="tier ram" class:on={lit("copy", "cpu", "nvme")}>
        <rect x="20" y="128" width="200" height="60" />
        <text x="34" y="152" class="title">PINNED RAM</text>
        <text x="34" y="174">warm experts</text>
      </g>
      <g class="tier cache" class:on={lit("cpu")}>
        <rect x="280" y="128" width="180" height="60" />
        <text x="294" y="152" class="title">CPU · 8 cores</text>
        <text x="294" y="174">computes host hits</text>
      </g>
      <g class="tier disk" class:on={lit("nvme")}>
        <rect x="20" y="230" width="200" height="60" />
        <text x="34" y="254" class="title">NVMe</text>
        <text x="34" y="276">every expert</text>
      </g>

      <g class="wires">
        <path d="M105,230 V188" class:on={lit("nvme")} />
        <path d="M105,128 V86" class:on={lit("copy", "nvme")} />
        <path d="M220,158 H280" class:on={lit("cpu")} />
        <path d="M370,128 V86" class:on={lit("cpu")} />
      </g>
      <text x="115" y="213" class="wire-label" class:on={lit("nvme")}
        >direct read</text
      >
      <text x="115" y="112" class="wire-label" class:on={lit("copy", "nvme")}
        >PCIe · 2.7 MB</text
      >
      <text x="380" y="112" class="wire-label" class:on={lit("cpu")}
        >10 KB row</text
      >

      {#key selected}
        <circle
          r="6"
          class="dot"
          cx={motion ? 0 : start[0]}
          cy={motion ? 0 : start[1]}
        >
          {#if motion}<animateMotion
              dur={selected === "nvme" ? "1.6s" : "1.1s"}
              repeatCount="indefinite"
              path={routes[selected]}
            />{/if}
        </circle>
      {/key}
    </svg>

    <div class="readout" aria-live="polite">
      <div class="cost">{path.cost}</div>
      <div class="moves">Moves: {path.moves}</div>
      {#if path.detail}<div class="detail">{path.detail}</div>{/if}
    </div>
  </div>

  <div class="ladder" role="group" aria-label="Cost of each case">
    {#each expertPaths as p}
      <div class="rung" class:on={p.key === selected}>
        <button
          type="button"
          aria-pressed={p.key === selected}
          onclick={() => (selected = p.key)}
        >
          <span class="name">{p.label}</span>
          <span class="bar"
            ><i
              class:illustrative={p.illustrative}
              style={`width:${Math.max(0.5, (p.micros / max) * 100)}%`}
            ></i></span
          >
          <span class="value">{p.micros ? `${p.micros} µs` : "0"}</span>
        </button>
      </div>
    {/each}
  </div>
</Fig>

<style>
  .layout {
    display: grid;
    grid-template-columns: minmax(0, 3fr) minmax(0, 2fr);
    gap: 1rem;
    align-items: center;
  }
  svg {
    display: block;
    width: 100%;
    height: auto;
    font-family: var(--font-code);
  }
  .tier rect {
    fill: none;
    stroke: var(--line);
    stroke-width: 1.5;
  }
  .tier text {
    fill: var(--ink-3);
    font-size: 15px;
  }
  .tier .title {
    font-weight: 600;
  }
  .tier.on rect {
    stroke: var(--t);
  }
  .tier.on text {
    fill: var(--ink);
  }
  .tier.on .title {
    fill: var(--t);
  }
  .gpu {
    --t: var(--tone-gpu);
  }
  .ram {
    --t: var(--tone-ram);
  }
  .cache {
    --t: var(--tone-cache);
  }
  .disk {
    --t: var(--tone-disk);
  }
  .wires path {
    fill: none;
    stroke: var(--line);
    stroke-width: 2;
  }
  .wires path.on {
    stroke: var(--ink);
    stroke-width: 2.5;
  }
  .wire-label {
    fill: var(--ink-3);
    font-size: 13px;
  }
  .wire-label.on {
    fill: var(--ink);
  }
  .dot {
    fill: var(--tone-hot);
  }
  .cost {
    color: var(--ink);
    font: 600 1.35rem / 1.2 var(--font-code);
  }
  .moves {
    margin-top: 0.35rem;
    color: var(--ink);
    font: 0.75rem var(--font-code);
  }
  .detail {
    margin-top: 0.5rem;
    color: var(--ink-2);
    font-size: 0.85rem;
    line-height: 1.45;
  }
  .ladder {
    margin-top: 0.75rem;
  }
  .ladder button {
    display: grid;
    grid-template-columns: minmax(8rem, 11rem) minmax(0, 1fr) 4.5rem;
    gap: 0.75rem;
    align-items: center;
    width: 100%;
    min-height: 2.25rem;
    padding: 0;
    border: 0;
    background: none;
    color: var(--ink-2);
    font: 0.7rem var(--font-code);
    text-align: left;
    cursor: pointer;
  }
  .rung.on button {
    color: var(--ink);
  }
  .bar {
    height: 0.6rem;
    background: var(--band);
  }
  .bar i {
    display: block;
    height: 100%;
    background: var(--ink-3);
  }
  .rung.on .bar i {
    background: var(--tone-hot);
  }
  .bar i.illustrative {
    background: repeating-linear-gradient(
      135deg,
      var(--ink-3) 0 3px,
      transparent 3px 6px
    );
  }
  .rung.on .bar i.illustrative {
    background: repeating-linear-gradient(
      135deg,
      var(--tone-hot) 0 3px,
      transparent 3px 6px
    );
  }
  .value {
    text-align: right;
    font-variant-numeric: tabular-nums;
  }
  @media (max-width: 600px) {
    .layout {
      grid-template-columns: 1fr;
    }
    .ladder button {
      grid-template-columns: 7.5rem minmax(0, 1fr) 3.75rem;
      gap: 0.5rem;
    }
  }
</style>
