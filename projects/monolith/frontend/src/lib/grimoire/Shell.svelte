<script>
  // Responsive frame for the grimoire body. On wide viewports (>= 880px) the
  // list routes and the reading routes share one two-pane frame: a contextual
  // index on the left, the reading surface (child route) on the right, so there
  // is never a dead half-screen. Below 880px it collapses to the classic stacked
  // master-detail: only the child route renders, with its own back affordance.
  //
  // The pane widths live here alone: child routes render their content and never
  // reinvent the frame. The Library root (no list context) always renders
  // full-width.
  import { getContext } from "svelte";
  import { page } from "$app/stores";
  import EntityIndexList from "./EntityIndexList.svelte";
  import SectionTree from "./SectionTree.svelte";

  let { children } = $props();
  const ctx = getContext("grimoire");

  // Which list belongs in the left pane, inferred from the route. We match the
  // SvelteKit route id (literal segments like `[campaign]`), not the resolved
  // pathname, so a campaign or book slug that happens to contain "entities" or
  // "book" cannot misclassify the frame. Library (the campaign root) has no list
  // pane and stays full-width.
  const context = $derived.by(() => {
    const id = $page.route.id ?? "";
    if (id.includes("/entity")) return "entities"; // covers /entities and /entity/[id]
    if (id.includes("/book/")) return "book";
    return "library";
  });
  const twoPane = $derived(context !== "library");
</script>

{#if ctx.isDesktop && twoPane}
  <div class="shell">
    <aside class="pane-list">
      {#if context === "entities"}
        <EntityIndexList />
      {:else if context === "book"}
        <SectionTree />
      {/if}
    </aside>
    <section class="pane-read">
      {@render children()}
    </section>
  </div>
{:else}
  {@render children()}
{/if}

<style>
  .shell {
    display: grid;
    grid-template-columns: 24rem minmax(0, 1fr);
    height: 100%;
    overflow: hidden;
  }

  .pane-list {
    height: 100%;
    overflow-y: auto;
    border-right: var(--border-thin);
  }

  .pane-read {
    height: 100%;
    overflow-y: auto;
  }

  /* A touch wider on very large screens, still within the ~22-26rem band. */
  @media (min-width: 1400px) {
    .shell {
      grid-template-columns: 26rem minmax(0, 1fr);
    }
  }
</style>
