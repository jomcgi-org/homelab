<script>
  import { page } from "$app/state";
  import { TechnicalDrawingChrome } from "$lib/public/components";
  import "$lib/public/styles/technical-drawing.css";
  import Trail from "./Trail.svelte";

  let { children } = $props();

  // The trail is one box partitioned by rules, the same way a figure's
  // parts are: site, then the section, then the page being read.
  const pageName = $derived(page.data.title ?? "");
</script>

<svelte:head>
  <meta name="robots" content="noindex, nofollow" />
</svelte:head>

<TechnicalDrawingChrome>
  {#snippet trail()}
    <Trail page={pageName} />
  {/snippet}
</TechnicalDrawingChrome>

{@render children()}
