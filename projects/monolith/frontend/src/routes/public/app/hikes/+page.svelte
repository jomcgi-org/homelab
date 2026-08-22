<script>
  import { browser } from "$app/environment";
  import HikesApp from "./HikesApp.svelte";

  let { data } = $props();
</script>

<svelte:head>
  <title>Hike planner, Scotland walks by weather window</title>
  <meta
    name="description"
    content="A map of Scottish hill walks coloured by effort and filtered by viable weather windows from the met.no forecast."
  />
</svelte:head>

<h1 class="sr-only">Hike planner, Scotland walks by weather window</h1>
<p>Filter Scottish hill walks by distance, weather, and viewing location.</p>

{#if browser}
  <HikesApp {data} />
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
