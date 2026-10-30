<script>
  // The friends tier is one sheet: the technical-drawing tokens ground every
  // screen (the Grimoire campaign screens read them through the bridge in
  // $lib/grimoire/theme.css), and the reader's persisted scheme applies
  // before paint, as on the public tier.
  import "$lib/public/styles/technical-drawing.css";
  import SchemePreference from "$lib/public/components/SchemePreference.svelte";
  let { children } = $props();
</script>

<svelte:head>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link
    rel="preconnect"
    href="https://fonts.gstatic.com"
    crossorigin="anonymous"
  />
  <link
    href="https://fonts.googleapis.com/css2?family=Schibsted+Grotesk:wght@400;500;600;700&display=swap"
    rel="stylesheet"
  />
</svelte:head>

<SchemePreference />
<div class="td friends">{@render children()}</div>

<style>
  .friends {
    min-height: 100vh;
    background: var(--sheet);
    color: var(--ink);
    font-family: var(--font-ui);
    line-height: 1.5;
  }
</style>
