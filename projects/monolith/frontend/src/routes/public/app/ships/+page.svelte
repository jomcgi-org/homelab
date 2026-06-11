<script>
  import { onMount } from "svelte";
  import { invalidateAll } from "$app/navigation";
  import ShipsMap from "$lib/public/components/ships/ShipsMap.svelte";

  let { data } = $props();

  let vessels = $derived(data.snapshot?.vessels ?? []);

  onMount(() => {
    // Live updates: re-run the SSR load on a timer. The browser hits the same
    // page route, which re-fetches the snapshot server-side. No client-side
    // call to /api/ships/* ever happens.
    const refresh = setInterval(() => invalidateAll(), 120_000);
    return () => clearInterval(refresh);
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
  <!-- The visual heading is the breadcrumb chip inside ShipsMap; keep a real
       (visually hidden) h1 so the page still has a heading for SEO + a11y. -->
  <h1 class="sr-only">Live ships, AIS vessel tracker</h1>
  <ShipsMap {vessels} />
</div>

<style>
  /* Full-bleed: the map fills everything under the global nav, edge to edge.
     ShipsMap's .map-wrap is absolutely positioned, so this is its containing
     block. */
  .ships-page {
    position: relative;
    /* No site nav on /app/* routes, so the map owns the whole viewport.
       100dvh tracks the dynamic viewport (mobile browser chrome) so the
       bottom-anchored legend never falls off-screen. 100vh is the fallback. */
    height: 100vh;
    height: 100dvh;
    overflow: hidden;
    background: var(--cream);
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
</style>
