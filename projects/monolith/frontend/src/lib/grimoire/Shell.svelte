<script>
  // Responsive frame for the grimoire body. On wide viewports (>= 880px) the
  // entities routes get a two-pane frame: an index list on the left, the
  // reading surface (child route) on the right, so there is never a dead
  // half-screen. Below 880px it collapses to the classic stacked
  // master-detail: only the child route renders, with its own back affordance.
  //
  // The pane widths live here alone: child routes render their content and never
  // reinvent the frame. The Library root and the book route (the reader owns
  // its own Chapters nav dropdown instead of a permanent TOC pane) have no
  // list context and always render full-width.
  import { getContext } from "svelte";
  import { page } from "$app/stores";
  import EntityIndexList from "./EntityIndexList.svelte";

  let { children } = $props();
  const ctx = getContext("grimoire");

  // Only the entities routes get a left pane now. We match the SvelteKit
  // route id (literal segments like `[campaign]`), not the resolved
  // pathname, so a campaign slug that happens to contain "entity" cannot
  // misclassify the frame.
  const twoPane = $derived(($page.route.id ?? "").includes("/entity"));
</script>

{#if ctx.isDesktop && twoPane}
  <div class="shell">
    <aside class="pane-list">
      <EntityIndexList />
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
