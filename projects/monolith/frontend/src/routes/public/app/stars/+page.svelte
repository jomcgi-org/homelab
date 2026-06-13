<script>
  import { onMount } from "svelte";
  import { invalidateAll } from "$app/navigation";
  import StarsMap from "$lib/public/components/stars/StarsMap.svelte";

  let { data } = $props();

  let sites = $derived(data.snapshot?.sites ?? []);
  let count = $derived(data.snapshot?.count ?? 0);
  // sites is already sorted by best_score descending, so the head is the best.
  let topScore = $derived(
    sites.length ? Math.round(sites[0].best_score ?? 0) : null,
  );

  // A coarse "current time" signal: it advances the "updated Xm ago" label and
  // lets StarsMap drop hours that elapse on a long-open page (see the tick in
  // onMount).
  let nowMs = $state(Date.now());

  // Relative age of the snapshot, mirroring the homepage's formatAgo (a local
  // helper, not a shared export). Parameterized on nowMs so it ticks.
  function formatAgo(iso, now) {
    const then = Date.parse(iso);
    if (!Number.isFinite(then)) return null;
    const minutes = Math.max(0, Math.round((now - then) / 60_000));
    if (minutes < 60) return `${minutes}m`;
    const hours = Math.round(minutes / 60);
    if (hours < 48) return `${hours}h`;
    return `${Math.round(hours / 24)}d`;
  }

  let agoLabel = $derived(formatAgo(data.snapshot?.fetched_at, nowMs));

  onMount(() => {
    // Live updates: re-run the SSR load every 30 min (the refresh job runs
    // 3-hourly). Same pattern as /app/hikes.
    const refresh = setInterval(() => {
      nowMs = Date.now();
      invalidateAll();
    }, 30 * 60_000);
    // Lower-frequency tick so the age label advances and elapsed hours drop out
    // of the open card without waiting for the data refetch; nothing here needs
    // sub-minute resolution.
    const clockTick = setInterval(() => (nowMs = Date.now()), 5 * 60_000);
    return () => {
      clearInterval(refresh);
      clearInterval(clockTick);
    };
  });
</script>

<svelte:head>
  <title>Dark-sky stargazing map, Scotland viewing windows</title>
  <meta
    name="description"
    content="A map of curated Scottish dark-sky sites scored by upcoming viewing windows from the met.no forecast."
  />
</svelte:head>

<div class="stars-page">
  <h1 class="sr-only">Dark-sky stargazing map, Scotland viewing windows</h1>

  <StarsMap {sites} {nowMs} />

  <!-- Floating header: breadcrumb + headline stats, top-left clear of the map
       chrome (mirrors the hikes control head). -->
  <div class="controls">
    <div class="panel control-head">
      <div class="crumb-row">
        <nav class="crumb" aria-label="Breadcrumb">
          <a class="crumb-home" href="https://jomcgi.dev/"
            >jomcgi.dev<span class="crumb-arrow" aria-hidden="true">&nearr;</span
            ></a
          >
          <span class="crumb-sep">/</span>
          <span class="crumb-name">stars</span>
        </nav>
        <p class="stats">
          {count} dark-sky sites{#if topScore != null}
            &middot; best score {topScore}{/if}{#if agoLabel}
            &middot; updated {agoLabel} ago{/if}
        </p>
      </div>
    </div>

    {#if count === 0}
      <div class="panel empty-state" role="status">
        No viewing windows above score 60 in the next few nights. Check back
        after the next forecast refresh.
      </div>
    {/if}
  </div>
</div>

<style>
  /* Full-bleed, map-first (same shell as /app/ships + /app/hikes): the map owns
     the viewport and every control floats over it. StarsMap's .map-wrap is
     absolutely positioned, so this is its containing block. --ink is the dark
     base so the load flash matches the night basemap, not a cream flash. */
  .stars-page {
    position: relative;
    height: 100vh;
    height: 100dvh;
    overflow: hidden;
    background: var(--ink);
    color: var(--ink);
  }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0 0 0 0);
    white-space: nowrap;
    border: 0;
  }

  /* Floating control stack, top-left, clear of the map's own chrome. */
  .controls {
    position: absolute;
    top: 16px;
    left: 16px;
    z-index: 5;
    display: flex;
    flex-direction: column;
    gap: 10px;
    width: min(420px, calc(100% - 32px));
  }

  /* Flat sharp-bordered overlay, matching the ships + hikes map overlays:
     paper bg, 2px ink border, no border-radius. */
  .panel {
    background: var(--paper);
    border: 2px solid var(--ink);
    padding: 12px;
  }

  .control-head {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  .crumb-row {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 8px 14px;
    flex-wrap: wrap;
  }

  .crumb {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    font-family: var(--mono);
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }

  .crumb-home {
    color: var(--ink);
    text-decoration: underline;
    text-decoration-color: var(--blue);
    text-decoration-thickness: 2px;
    text-decoration-skip-ink: none;
    text-underline-offset: 2px;
    padding: 0 2px;
    transition: background 140ms ease;
  }

  .crumb-home:hover,
  .crumb-home:focus-visible {
    background: linear-gradient(transparent 56%, var(--accent) 56%);
    text-decoration-color: var(--ink);
  }

  .crumb-arrow {
    font-size: 0.85em;
    margin-left: 1px;
  }

  .crumb-sep {
    color: var(--ink-3);
  }

  .stats {
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    color: var(--ink-2);
  }

  .empty-state {
    font-family: var(--mono);
    font-size: 12px;
    line-height: 1.5;
    letter-spacing: 0.02em;
    color: var(--ink-2);
  }

  @media (max-width: 640px) {
    .controls {
      top: 12px;
      left: 12px;
      width: calc(100% - 24px);
    }
  }
</style>
