<script>
  import { page } from "$app/state";
  import { TechnicalDrawingChrome } from "$lib/public/components";
  import "$lib/public/styles/technical-drawing.css";
  import Trail from "./Trail.svelte";

  let { children } = $props();

  // The trail is one box partitioned by rules, the same way a figure's
  // parts are: site, then the section, then the page being read.
  const pageName = $derived(page.data.title ?? "");
  const ownsChrome = $derived(
    /^\/(public\/)?slop\/factory(\/|$)/.test(page.url.pathname),
  );
</script>

<svelte:head>
  <meta name="robots" content="noindex, nofollow" />
</svelte:head>

<!-- Factory views put the trail, tabs, and scheme toggle in one masthead row,
     so the fixed chrome would duplicate both the trail and the toggle. -->
{#if !ownsChrome}
  <TechnicalDrawingChrome>
    {#snippet trail()}
      <Trail page={pageName} />
    {/snippet}
  </TechnicalDrawingChrome>
{/if}

{@render children()}
