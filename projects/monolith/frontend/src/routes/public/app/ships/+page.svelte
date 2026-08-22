<script>
  import { browser } from "$app/environment";
  import ShipsApp from "./ShipsApp.svelte";

  let { data } = $props();
</script>

<svelte:head>
  <title>Live ships, AIS vessel tracker</title>
  <meta
    name="description"
    content="A live map of vessel positions from AIS, with client-side dead reckoning between snapshots."
  />
</svelte:head>

<h1 class="sr-only">Live ships, AIS vessel tracker</h1>
<p>Real-time tracking of vessels using AIS data.</p>

{#if browser}
  <ShipsApp {data} />
{/if}

<style>
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
