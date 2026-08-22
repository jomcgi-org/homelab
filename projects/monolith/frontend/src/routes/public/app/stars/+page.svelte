<script>
  import { browser } from "$app/environment";
  import StarsApp from "./StarsApp.svelte";

  let { data } = $props();
  const description =
    "Scotland's dark-sky sites ranked by forecast clear, dark hours.";
</script>

<svelte:head>
  <title>Dark-sky stargazing map, Scotland viewing windows</title>
  <meta
    name="description"
    content="A map of curated Scottish dark-sky sites scored by upcoming viewing windows from the met.no forecast, with a historical clear-dark-hours layer by month."
  />
</svelte:head>

<h1 class="sr-only">Dark-sky stargazing map, Scotland viewing windows</h1>
<p class="sr-only">{description}</p>

{#if browser}
  <StarsApp {data} />
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
