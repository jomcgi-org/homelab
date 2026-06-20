<script>
  // Day-view elevation sparkline, a faithful port of the original React
  // ElevationSparkline (fillHeight mode): a thin polyline profile that
  // x-stretches to fill its cell, with ↓min / ↑max labels, a baseline, and a
  // round position marker dot pinned to the current photo's progress along the
  // route. The marker is an absolutely positioned div (a true circle) rather than
  // an SVG node, since the viewBox is x-stretched (preserveAspectRatio="none").
  //
  // `data` is the sampled elevation array; `currentIndex` is the marker's index
  // into it; `accentColor` tints the marker (the day colour).
  let { data = [], currentIndex = 0, accentColor = "#dc2626" } = $props();

  const min = $derived(data.length ? Math.min(...data) : 0);
  const max = $derived(data.length ? Math.max(...data) : 1);
  const range = $derived(max - min || 1);

  // Normalised 0..100 viewBox (height 100), matching the original formula:
  // y = 100 - ((val - min) / range) * (100 - 4) - 2.
  const points = $derived(
    data
      .map((val, i) => {
        const x = data.length > 1 ? (i / (data.length - 1)) * 100 : 0;
        const y = 100 - ((val - min) / range) * 96 - 2;
        return `${x},${y}`;
      })
      .join(" "),
  );

  const markerLeftPercent = $derived(
    data.length > 1 ? (currentIndex / (data.length - 1)) * 100 : 50,
  );
  const markerTopPercent = $derived(
    data[currentIndex] != null
      ? 100 - ((data[currentIndex] - min) / range) * 96 - 2
      : 50,
  );
</script>

{#if data.length}
  <div class="wrap">
    <div class="minmax">
      <span>&darr; {Math.round(min)}m</span>
      <span>&uarr; {Math.round(max)}m</span>
    </div>
    <div class="svgbox">
      <svg
        width="100%"
        height="100%"
        viewBox="0 0 100 100"
        preserveAspectRatio="none"
        style="display:block;position:absolute;inset:0"
        aria-hidden="true"
      >
        <line
          x1="0"
          y1="99"
          x2="100"
          y2="99"
          stroke="#e5e7eb"
          stroke-width="1"
          vector-effect="non-scaling-stroke"
        />
        <polyline
          {points}
          fill="none"
          stroke="#1a1a1a"
          stroke-width="2.5"
          stroke-linecap="round"
          stroke-linejoin="round"
          vector-effect="non-scaling-stroke"
        />
      </svg>
      <div
        class="marker"
        style={`left:${markerLeftPercent}%;top:${markerTopPercent}%;background:${accentColor}`}
      ></div>
    </div>
  </div>
{/if}

<style>
  .wrap {
    position: absolute;
    inset: 0;
    display: flex;
    flex-direction: column;
  }
  .minmax {
    display: flex;
    justify-content: space-between;
    margin-bottom: 4px;
    font-family: monospace;
    font-size: 9px;
    font-weight: 600;
    color: #9ca3af; /* nosemgrep: svelte-hardcoded-color-in-style */
  }
  .svgbox {
    flex-grow: 1;
    position: relative;
    min-height: 0;
  }
  .marker {
    position: absolute;
    transform: translate(-50%, -50%);
    width: 10px;
    height: 10px;
    border-radius: 50%;
    border: 2px solid white;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.3);
  }
</style>
