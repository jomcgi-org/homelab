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

<div class="ships-page">
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
</style>
