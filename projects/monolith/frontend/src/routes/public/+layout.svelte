<script>
  import "$lib/public/styles/design-system.css";
  import { personLdScript } from "$lib/public/seo.js";
  let { data, children } = $props();
</script>

<svelte:head>
  <!-- Site-wide machine-readable identity (schema.org Person). Lives in the
       layout so every public crawl entry point carries the same typed identity. -->
  {@html personLdScript}
  <link rel="icon" type="image/svg+xml" href="/favicon.svg" />
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link
    rel="preconnect"
    href="https://fonts.gstatic.com"
    crossorigin="anonymous"
  />
  <link
    href="https://fonts.googleapis.com/css2?family=Hanken+Grotesk:ital,wght@0,300;0,400;0,500;0,600;1,300;1,400&family=Instrument+Serif:ital@0;1&family=JetBrains+Mono:wght@400;500;600;700&display=swap"
    rel="stylesheet"
  />
</svelte:head>

{#if data.maintenanceBanner}
  <div class="maintenance-banner" role="status">
    {data.maintenanceBanner}
  </div>
{/if}

{@render children()}

<style>
  .maintenance-banner {
    box-sizing: border-box;
    width: 100%;
    padding: 12px 16px;
    border-bottom: 2px solid var(--ink);
    background: var(--accent);
    color: var(--ink);
    font-family: var(--sans);
    font-size: 15px;
    font-weight: 400;
    line-height: 1.4;
    text-align: center;
  }
</style>
