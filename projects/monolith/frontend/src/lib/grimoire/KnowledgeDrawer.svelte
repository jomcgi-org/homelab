<script>
  import { onMount } from "svelte";
  import KnowledgeDetails from "./KnowledgeDetails.svelte";
  let { endpoint, entityId, close } = $props();
  let entity = $state(null);
  let failure = $state("");
  async function load() {
    try {
      const response = await fetch(`${endpoint}?entity=${entityId}`);
      const result = await response.json();
      if (!response.ok) throw new Error(result.error);
      entity = result;
      failure = "";
    } catch {
      entity = null;
      failure = "This knowledge is no longer available to you.";
    }
  }
  onMount(() => {
    load();
    const timer = setInterval(load, 4000);
    return () => clearInterval(timer);
  });
</script>

<section role="region" aria-label="Knowledge detail" class="grimoire drawer">
  <button onclick={close}>Close knowledge</button>
  {#if failure}<p role="alert">{failure}</p>{:else if entity}<p>
      {entity.entity_type}
    </p>
    <h2>{entity.name}</h2>
    <KnowledgeDetails {entity} />{:else}<p>Loading knowledge…</p>{/if}
</section>

<style>
  .drawer {
    position: fixed;
    right: 0;
    top: 0;
    bottom: 0;
    width: min(440px, 100%);
    padding: 24px;
    box-sizing: border-box;
    overflow-y: auto;
    background: var(--grim-surface);
    color: var(--grim-ink);
    border-left: 1px solid var(--grim-line);
    box-shadow: -12px 0 40px #0002;
    z-index: 20;
  }
  button {
    font: inherit;
    padding: 12px;
    background: var(--grim-surface);
    color: var(--grim-ink);
    border: 1px solid var(--grim-line);
    cursor: pointer;
  }
</style>
