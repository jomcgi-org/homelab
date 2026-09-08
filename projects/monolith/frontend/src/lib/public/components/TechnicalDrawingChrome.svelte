<script>
  import SchemeToggle from "./SchemeToggle.svelte";

  let { trail } = $props();
</script>

<div class="td chrome">
  <div class="trail-slot">
    {@render trail()}
  </div>
  <SchemeToggle />
</div>

<style>
  /* On a wide screen only the scheme switch floats, top right; the trail
     is docked at the top of the index column by the pages themselves so
     it lines up with the index and sticks with it. On a phone there is
     no index column to dock in, so the trail joins the switch in one row
     in the flow at the top of the page and scrolls away with it. */
  .chrome {
    position: fixed;
    top: 1.5rem;
    right: 1.2rem;
    z-index: 1000;
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 1rem;
  }

  .trail-slot {
    display: none;
    min-width: 0;
  }

  @media (max-width: 760px) {
    .chrome {
      position: static;
      padding: 1rem 1em 0;
      background: var(--sheet);
    }

    .trail-slot {
      display: block;
    }
  }
</style>
