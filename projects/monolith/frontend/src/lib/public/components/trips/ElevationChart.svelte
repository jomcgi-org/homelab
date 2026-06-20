<script>
  // Filled-area elevation sparkline. `series` is an already-sampled array of
  // elevations (see lib/trips/trip.js elevationSeries). Pure SVG, no deps.
  // `cursor` (optional) is a 0..1 fraction along the route; when set, a vertical
  // line is drawn at that x position to track the scrubber's current photo.
  // `cursorColor` lets the page tint it to the day colour. The viewBox uses
  // preserveAspectRatio="none" (the area path is x-stretched), so the cursor is
  // a vertical line only: a circle would render as a distorted ellipse.
  let {
    series = [],
    height = 28,
    min = null,
    max = null,
    color = "var(--ink)",
    cursor = null,
    cursorColor = "var(--ink)",
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

  // Clamp the cursor fraction to [0,1] and map to an x in 0..100 viewBox units.
  const cursorX = $derived(
    cursor == null ? null : Math.max(0, Math.min(1, cursor)) * 100,
  );
</script>

{#if path}
  <svg
    viewBox={`0 0 100 ${height}`}
    preserveAspectRatio="none"
    style={`width:100%;height:${height}px;display:block`}
    aria-hidden="true"
  >
    <path d={path} fill={color} fill-opacity="0.85" />
    {#if cursorX != null}
      <line
        x1={cursorX}
        y1="0"
        x2={cursorX}
        y2={height}
        stroke={cursorColor}
        stroke-width="1.5"
        vector-effect="non-scaling-stroke"
      />
    {/if}
  </svg>
{/if}
