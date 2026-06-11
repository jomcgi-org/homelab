<script>
  import { onMount } from "svelte";
  import { invalidateAll } from "$app/navigation";
  import ShipsMap from "$lib/public/components/ships/ShipsMap.svelte";

  let { data } = $props();

  let vessels = $derived(data.snapshot?.vessels ?? []);
  let count = $derived(data.snapshot?.count ?? vessels.length);

  // "updated Ns ago": reset the clock each time a fresh snapshot lands, then
  // tick a counter so the chip reads live without re-running the load.
  let lastUpdated = $state(Date.now());
  let secondsAgo = $state(0);

  // data is a fresh object on every invalidateAll, so referencing it here
  // re-runs the effect when the server load returns new vessels.
  $effect(() => {
    void data.snapshot;
    lastUpdated = Date.now();
    secondsAgo = 0;
  });

  onMount(() => {
    // Live updates: re-run the SSR load on a timer. The browser hits the same
    // page route, which re-fetches the snapshot server-side. No client-side
    // call to /api/ships/* ever happens.
    const refresh = setInterval(() => invalidateAll(), 120_000);
    const tick = setInterval(() => {
      secondsAgo = Math.round((Date.now() - lastUpdated) / 1000);
    }, 1000);
    return () => {
      clearInterval(refresh);
      clearInterval(tick);
    };
  });
</script>

<svelte:head>
  <title>Live ships, AIS vessel tracker</title>
  <meta
    name="description"
    content="A live map of vessel positions from AIS, with client-side dead reckoning between snapshots."
  />
</svelte:head>

<div class="ships-page">
  <header class="ships-head wrap">
    <div class="ships-head-text">
      <p class="eyebrow">Live AIS</p>
      <h1 class="ships-title display">Ships</h1>
      <p class="ships-sub">
        Vessel positions, dead-reckoned between two-minute snapshots.
      </p>
    </div>
    <div class="ships-status" aria-live="polite">
      <span class="ships-dot" aria-hidden="true"></span>
      <span class="ships-status-label">
        {count} vessels &middot; updated {secondsAgo}s ago
      </span>
    </div>
  </header>

  <div class="ships-stage">
    <ShipsMap {vessels} />
  </div>
</div>

<style>
  .ships-page {
    display: flex;
    flex-direction: column;
    height: calc(100vh - 48px);
    background: var(--cream);
    color: var(--ink);
  }

  .ships-head {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 24px;
    padding-top: 24px;
    padding-bottom: 20px;
    flex-wrap: wrap;
  }

  .ships-title {
    font-size: clamp(40px, 7vw, 72px);
    margin: 4px 0 6px;
  }

  .ships-sub {
    font-family: var(--mono);
    font-size: 13px;
    color: var(--ink-3);
    letter-spacing: 0.02em;
  }

  .ships-status {
    display: inline-flex;
    align-items: center;
    gap: 10px;
    padding: 10px 14px;
    background: var(--paper);
    border: 2px solid var(--ink);
    box-shadow: var(--shadow-hard-sm);
    font-family: var(--mono);
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    white-space: nowrap;
  }

  .ships-dot {
    width: 10px;
    height: 10px;
    border-radius: 999px;
    background: var(--green);
    border: 1px solid var(--ink);
    animation: ships-pulse 1.6s ease-in-out infinite;
  }

  @keyframes ships-pulse {
    0%,
    100% {
      opacity: 1;
    }
    50% {
      opacity: 0.35;
    }
  }

  .ships-stage {
    flex: 1;
    position: relative;
    min-height: 0;
    border-top: 2px solid var(--ink);
  }

  @media (prefers-reduced-motion: reduce) {
    .ships-dot {
      animation: none;
    }
  }
</style>
