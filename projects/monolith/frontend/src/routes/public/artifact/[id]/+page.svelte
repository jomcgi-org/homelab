<script>
  import { onMount, onDestroy } from "svelte";
  import { page } from "$app/state";
  import "$lib/public/styles/technical-drawing.css";

  const id = $derived(page.params.id);
  // Polls may override this URL until the route id changes, as before.
  let rawUrl = $derived(`/artifact/${id}/raw`);

  let lastVersion = null;
  let pollInterval = null;

  onMount(() => {
    pollInterval = setInterval(async () => {
      try {
        const res = await fetch(`/artifact/${id}/version`);
        if (!res.ok) return;
        const data = await res.json();
        const v = data?.version;
        if (!v) return;
        if (lastVersion !== null && v !== lastVersion) {
          // Artifact has been updated: force the iframe to reload by bumping
          // the cache-bust param on rawUrl.
          rawUrl = `/artifact/${id}/raw?v=${encodeURIComponent(v)}`;
        }
        lastVersion = v;
      } catch {
        // Network error or JSON parse failure: ignore silently, try again next tick.
      }
    }, 3000);
  });

  onDestroy(() => {
    clearInterval(pollInterval);
  });
</script>

<svelte:head>
  <title>Artifact</title>
  <style>
    /* Outrank the shared body:has(.td) rule regardless of chunk order. */
    body:has(.td.artifact-sheet) {
      margin: 0;
      padding: 0;
      overflow: hidden;
    }
  </style>
</svelte:head>

<!--
  SECURITY INVARIANT (ADR 024): sandbox MUST be exactly "allow-scripts".
  Do NOT add allow-same-origin: that would re-grant the jomcgi.dev origin
  to artifact code, defeating the entire sandbox. Do NOT add allow-forms,
  allow-popups, or allow-top-navigation either.
-->
<main class="td sheet artifact-sheet">
  <iframe title="artifact" sandbox="allow-scripts" src={rawUrl}></iframe>
</main>

<style>
  /* Artifacts occupy the viewport. The sheet's padded frame and the mobile
     chrome row would inset or displace the iframe, so neither belongs here.
     The root layout continues to suppress navigation for /artifact/*.
     Theme tokens colour only this wrapper, never the isolated document. */
  .artifact-sheet {
    padding: 0;
  }

  iframe {
    width: 100%;
    height: 100vh;
    border: 0;
    display: block;
  }
</style>
