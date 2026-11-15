<script>
  import { routePreview } from "$lib/trips/route-preview.js";
  import { dayColor } from "$lib/trips/trip.js";
  let {
    days = [],
    color,
    label = "Trip route",
    failed = false,
    waitingForContext = false,
    onRetry,
  } = $props();
  const preview = $derived(routePreview(days));
</script>

<div class="route-preview" class:failed>
  {#if failed}
    <div class="recovery">
      <p>
        {waitingForContext
          ? "Waiting for graphics recovery."
          : "Interactive map unavailable."}
      </p>
      {#if onRetry}<button type="button" onclick={onRetry}>Retry map</button
        >{/if}
    </div>
  {/if}
  {#if preview.routes.length}
    <svg viewBox="0 0 640 320" role="img">
      <title>{label} preview from GPS points</title>
      {#each preview.routes as route (route.index)}
        <polyline
          points={route.points}
          fill="none"
          stroke={color ?? dayColor(route.index)}
          stroke-width="2"
          stroke-linecap="round"
          stroke-linejoin="round"
          vector-effect="non-scaling-stroke"
        />
        <circle
          cx={route.samples[0][0]}
          cy={route.samples[0][1]}
          r="3"
          fill={color ?? dayColor(route.index)}
        />
      {/each}
    </svg>
  {:else}
    <p class="extent">
      {#if preview.reason === "polar"}
        This route reaches a polar region. Explore it on the interactive map.
      {:else if preview.reason === "extent"}
        Explore this journey on the interactive map.
      {:else}
        No GPS points for this route.
      {/if}
    </p>
  {/if}
  <p class="caption">
    Route preview &middot; {preview.pointCount.toLocaleString("en-GB")} GPS {preview.pointCount ===
    1
      ? "point"
      : "points"}
    {#if preview.dayCount > 1}&middot; {preview.dayCount} days{/if}
  </p>
</div>

<style>
  .route-preview {
    position: absolute;
    inset: 1px;
    z-index: 1;
    overflow: hidden;
    background: var(--sheet);
    color: var(--ink);
    /* The preview has its own projection. Never send clicks to hidden routes.
       MapLibre's controls use z-index 2 and remain above this overlay. */
    pointer-events: auto;
    touch-action: pan-y;
  }
  svg {
    display: block;
    width: 100%;
    height: 100%;
  }
  .caption,
  .extent {
    font: 0.7rem/1.5 var(--font-code);
    margin: 0;
  }
  .caption {
    position: absolute;
    left: 0.75rem;
    bottom: 0.5rem;
    background: var(--sheet);
  }
  .extent {
    padding: 1.5rem;
  }
  .failed .extent {
    padding-top: 5rem;
  }
  .recovery {
    position: absolute;
    top: 0.75rem;
    left: 0.75rem;
    z-index: 1;
    max-width: calc(100% - 5rem);
    background: var(--sheet);
    font: 0.7rem/1.5 var(--font-code);
  }
  .recovery p {
    margin: 0 0 0.5rem;
  }
  button {
    background: var(--sheet);
    color: var(--ink);
    border: 1px solid var(--ink);
    padding: 0.35rem 0.6rem;
    font: inherit;
    cursor: pointer;
  }
  button:focus-visible {
    outline: 2px solid var(--ink);
    outline-offset: 2px;
  }
</style>
