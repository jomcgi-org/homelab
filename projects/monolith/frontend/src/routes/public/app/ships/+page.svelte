<script>
  import { browser } from "$app/environment";
  import ShipsApp from "./ShipsApp.svelte";
  import TechnicalDrawingChrome from "$lib/public/components/TechnicalDrawingChrome.svelte";
  import Trail from "$lib/public/components/Trail.svelte";
  import "$lib/public/styles/technical-drawing.css";
  import "$lib/public/ships/sheet.css";

  let { data } = $props();
</script>

<svelte:head>
  <title>Live ships, AIS vessel tracker</title>
  <meta
    name="description"
    content="A live map of vessel positions from AIS, with client-side dead reckoning between snapshots."
  />
</svelte:head>

{#snippet trail()}
  <Trail crumbs={[{ label: "ships" }, { label: "live" }]} />
{/snippet}

<main class="td sheet ships-sheet">
  <div class="ships-chrome"><TechnicalDrawingChrome {trail} /></div>
  <h1 class="sr-only">Live ships, AIS vessel tracker</h1>
  <p class="sr-only">
    Live vessel positions from AIS, the position broadcasts ships transmit.
  </p>

  {#if browser}
    <ShipsApp {data} />
  {/if}
</main>

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
