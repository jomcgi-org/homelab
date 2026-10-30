<script>
  import RootLayout from "../src/routes/+layout.svelte";
  import PrivateLayout from "../src/routes/private/+layout.svelte";
  import Execution from "../src/routes/private/factory/execution/+page.svelte";
  import Drain from "../src/routes/private/factory/execution/drain/+page.svelte";
  import { executionData } from "./execution-fixtures.js";
  let { view = "execution" } = $props();
</script>

<RootLayout>
  <PrivateLayout>
    {#if view === "drain"}
      <Drain />
    {:else}
      <Execution data={executionData} />
    {/if}
  </PrivateLayout>
</RootLayout>
