<script>
  // Filled-area elevation sparkline. `series` is an already-sampled array of
  // elevations (see lib/trips/trip.js elevationSeries). Pure SVG, no deps.
  let {
    series = [],
    height = 28,
    min = null,
    max = null,
    color = "var(--ink)",
  } = $props();

  const lo = $derived(min ?? (series.length ? Math.min(...series) : 0));
  const hi = $derived(max ?? (series.length ? Math.max(...series) : 1));

  const path = $derived.by(() => {
    if (series.length < 2) return "";
    const range = hi - lo || 1;
    let d = `M 0 ${height} `;
    series.forEach((e, i) => {
      const x = (i / (series.length - 1)) * 100;
      const y = height - ((e - lo) / range) * height;
      d += `L ${x.toFixed(2)} ${y.toFixed(2)} `;
    });
    d += `L 100 ${height} Z`;
    return d;
  });
</script>

{#if path}
  <svg
    viewBox={`0 0 100 ${height}`}
    preserveAspectRatio="none"
    style={`width:100%;height:${height}px;display:block`}
    aria-hidden="true"
  >
    <path d={path} fill={color} fill-opacity="0.85" />
  </svg>
{/if}
