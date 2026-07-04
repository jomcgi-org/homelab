<script>
  // On desktop the section tree lives in the Shell's left pane, so this route
  // (the right pane) prompts the reader to pick a section. On mobile it IS the
  // section tree, full-width, and tapping a section opens the chunk reader.
  import { getContext } from "svelte";
  import SectionTree from "$lib/grimoire/SectionTree.svelte";
  import PanePlaceholder from "$lib/grimoire/PanePlaceholder.svelte";

  const ctx = getContext("grimoire");
</script>

{#if ctx.isDesktop}
  <PanePlaceholder
    lead="Select a section"
    help="Pick a section from the list to start reading. Prev / next then walk the whole book in order."
  />
{:else}
  <SectionTree />
{/if}
