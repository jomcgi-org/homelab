<script>
  import KnowledgeDetails from "./KnowledgeDetails.svelte";
  import { projectionOf, scopeOf } from "./knowledge-fields.js";
  // One renderer for what a player is shown: the DM preview, the player feed
  // card and the grants page all go through it, so they cannot drift apart.
  let { knowledge } = $props();
  let projection = $derived(projectionOf(knowledge));
  let scope = $derived(scopeOf(knowledge));
</script>

<div class="reveal-projection" data-scope={scope}>
  <p class="identity">
    <strong>{projection.name}</strong> · {projection.entity_type}
  </p>
  {#if scope === "name_only"}
    <p>You recognize this name.</p>
  {:else}
    <KnowledgeDetails entity={projection} />
  {/if}
</div>
