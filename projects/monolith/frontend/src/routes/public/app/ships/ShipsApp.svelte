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
  /* The sheet reserves its chrome row; the existing map owns the remainder. */
  .ships-page {
    position: relative;
    min-height: 0;
    overflow: hidden;
    background: var(--sheet);
    color: var(--ink);
  }
</style>
