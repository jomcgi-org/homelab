<script>
  import Overview from "../src/routes/public/slop/factory/+page.svelte";
  import Activity from "../src/routes/public/slop/factory/activity/+page.svelte";
  import Context from "../src/routes/public/slop/factory/context/+page.svelte";
  import { fixture, page } from "./state.svelte.js";
</script>

<div class="public-theme" hidden></div>
{#key `${fixture.scenario}/${fixture.view}`}
  {#if fixture.view === "overview"}
    <Overview data={page.data} />
  {:else if fixture.view === "activity"}
    <Activity data={page.data} />
  {:else}
    <Context data={page.data} />
  {/if}
{/key}
<p id="preview-scope" tabindex="-1">
  Synthetic fixture preview. All records and measurements are invented.
</p>
