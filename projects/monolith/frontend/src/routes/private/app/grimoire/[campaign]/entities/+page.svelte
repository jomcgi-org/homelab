<script>
  // On desktop the entity index lives in the Shell's left pane, so this route
  // (the right pane) is the "nothing selected yet" prompt. On mobile it IS the
  // index, full-width, and tapping a row navigates to the detail screen.
  import { getContext } from "svelte";
  import EntityIndexList from "$lib/grimoire/EntityIndexList.svelte";
  import PanePlaceholder from "$lib/grimoire/PanePlaceholder.svelte";

  const ctx = getContext("grimoire");
</script>

{#if ctx.isDesktop}
  <PanePlaceholder
    lead="Select an entity"
    help="Pick a name from the index to see its stat block, relationships, and the pages it appears on."
  />
{:else}
  <EntityIndexList />
{/if}
