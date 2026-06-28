<script>
  import { onMount, onDestroy } from "svelte";
  import { page } from "$app/stores";

  $: id = $page.params.id;
  $: rawUrl = `/artifact/${id}/raw`;

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
    body {
      margin: 0;
      padding: 0;
      overflow: hidden;
    }
  </style>
</svelte:head>

<!--
  SECURITY INVARIANT (ADR 024): sandbox MUST be exactly "allow-scripts".
  Do NOT add allow-same-origin — that would re-grant the jomcgi.dev origin
  to artifact code, defeating the entire sandbox. Do NOT add allow-forms,
  allow-popups, or allow-top-navigation either.
-->
<iframe
  title="artifact"
  sandbox="allow-scripts"
  src={rawUrl}
  style="width:100%;height:100vh;border:0;display:block;"
></iframe>
