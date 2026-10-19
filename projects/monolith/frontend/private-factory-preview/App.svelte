<script>
  import Root from "../src/routes/+layout.svelte";
  import Private from "../src/routes/private/+layout.svelte";
  import Board from "../src/routes/private/factory/+page.svelte";
  import Escalations from "../src/routes/private/factory/escalations/+page.svelte";
  import Item from "../src/routes/private/factory/work-items/[id]/+page.svelte";
  let { view, data } = $props();
</script>

<Root
  ><Private>
    {#if view === "board"}<Board {data} />
    {:else if view === "escalations"}<Escalations {data} />
    {:else if view === "item"}<Item {data} />
    {:else}<p>Layout fixture</p>{/if}
  </Private></Root
>
