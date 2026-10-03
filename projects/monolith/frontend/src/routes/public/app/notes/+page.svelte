<script>
  import { browser } from "$app/environment";
  import { page } from "$app/state";
  import GraphView from "$lib/public/chat/GraphView.svelte";
</script>

<svelte:head>
  <title>Public notes · jomcgi.dev</title>
  <meta
    name="description"
    content="Browse my public knowledge graph and the notes behind the factory."
  />
</svelte:head>

<main>
  <h1>Public notes</h1>
  <a href="/slop/factory">Watch the factory</a>
  {#if browser}
    <div class="graph">
      <GraphView focusId={page.url.searchParams.get("focus")} />
    </div>
  {/if}
</main>

<style>
  main {
    padding: 24px;
  }
  h1 {
    margin: 0 0 8px;
  }
  .graph {
    height: 80dvh;
    min-height: 480px;
    margin-top: 24px;
  }
</style>
