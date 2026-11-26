<script>
  import Fig from "./Fig.svelte";
  import { expertPaths } from "./data.js";

  const max = Math.max(...expertPaths.map((p) => p.micros));
  const at = Object.fromEntries(expertPaths.map((p) => [p.key, p]));
</script>

<Fig title="Bandwidth constraints for expert tiers">
  <svg
    viewBox="0 0 480 300"
    role="img"
    aria-label={expertPaths.map((p) => `${p.label}: ${p.cost}`).join("; ")}
  >
    <g class="tier gpu">
      <rect x="20" y="16" width="440" height="70" />
      <text x="34" y="40" class="title">VRAM · 24 GB</text>
      <text x="34" y="64">dense weights · state · hot experts</text>
    </g>
    <g class="tier ram">
      <rect x="20" y="128" width="200" height="60" />
      <text x="34" y="152" class="title">PINNED RAM</text>
      <text x="34" y="174">warm experts</text>
    </g>
    <g class="tier cache">
      <rect x="280" y="128" width="180" height="60" />
      <text x="294" y="152" class="title">CPU · 8 cores</text>
      <text x="294" y="174">computes host hits</text>
    </g>
    <g class="tier disk">
      <rect x="20" y="230" width="200" height="60" />
      <text x="34" y="254" class="title">NVMe</text>
      <text x="34" y="276">every expert</text>
    </g>

    <g class="wires">
      <path d="M105,230 V188" />
      <path d="M105,128 V86" />
      <path d="M220,158 H280" />
      <path d="M370,128 V86" />
    </g>
    <text x="115" y="104" class="wire-label">copy 2.7 MB</text>
    <text x="115" y="119" class="wire-cost">{at.copy.cost}</text>
    <text x="115" y="206" class="wire-label">direct read</text>
    <text x="115" y="221" class="wire-cost">~0.45 ms, illustrative</text>
    <text x="380" y="104" class="wire-label">10 KB row</text>
    <text x="380" y="119" class="wire-cost">{at.cpu.cost}</text>
  </svg>

  <div class="ladder">
    {#each expertPaths as p}
      <div class="rung">
        <span class="name"
          >{p.label}{p.illustrative ? " (illustrative)" : ""}</span
        >
        <span class="bar"
          ><i
            class:illustrative={p.illustrative}
            style={`width:${Math.max(0.5, (p.micros / max) * 100)}%`}
          ></i></span
        >
        <span class="value">{p.micros ? `${p.micros} µs` : "0"}</span>
      </div>
    {/each}
  </div>

  {#snippet note()}
    Running a host hit on the CPU applies to one-token decode. The NVMe row is
    arithmetic for a 2.7 MB read plus the copy, not an engine measurement.
  {/snippet}
</Fig>

<style>
  svg {
    display: block;
    width: 100%;
    max-width: 30rem;
    height: auto;
    font-family: var(--font-code);
  }
  .tier rect {
    fill: none;
    stroke: var(--t);
    stroke-width: 1.5;
  }
  .tier text {
    fill: var(--ink);
    font-size: 15px;
  }
  .tier .title {
    fill: var(--t);
    font-weight: 600;
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
    stroke: var(--ink);
    stroke-width: 2;
  }
  .wire-label {
    fill: var(--ink-2);
    font-size: 13px;
  }
  .wire-cost {
    fill: var(--ink);
    font-size: 13px;
    font-weight: 600;
  }
  .ladder {
    display: grid;
    gap: 0.5rem;
    margin-top: 1rem;
  }
  .rung {
    display: grid;
    grid-template-columns: minmax(8rem, 11rem) minmax(0, 1fr) 4.5rem;
    gap: 0.75rem;
    align-items: center;
    color: var(--ink);
    font: 0.7rem var(--font-code);
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
  .bar i.illustrative {
    background: repeating-linear-gradient(
      135deg,
      var(--ink-3) 0 3px,
      transparent 3px 6px
    );
  }
  .value {
    text-align: right;
    font-variant-numeric: tabular-nums;
  }
  @media (max-width: 600px) {
    .rung {
      grid-template-columns: 7.5rem minmax(0, 1fr) 3.75rem;
      gap: 0.5rem;
    }
  }
</style>
