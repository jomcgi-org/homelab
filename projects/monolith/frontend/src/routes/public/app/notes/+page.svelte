<script>
  import { browser } from "$app/environment";
  import { page } from "$app/state";
  import GraphView from "$lib/public/notes/GraphView.svelte";
  import {
    SiteIndex,
    TechnicalDrawingChrome,
    Trail,
  } from "$lib/public/components";
  import "$lib/public/styles/technical-drawing.css";
</script>

<svelte:head>
  <title>Public notes · jomcgi.dev</title>
  <meta
    name="description"
    content="Browse my public knowledge graph and the notes behind the factory."
  />
</svelte:head>

{#snippet trail()}
  <Trail crumbs={[{ label: "notes" }]} />
{/snippet}

<TechnicalDrawingChrome {trail} />

<main class="td sheet notes-page">
  <div class="frame">
    <div class="journal">
      <aside class="spine">
        <div class="trail-dock">{@render trail()}</div>
        <SiteIndex here="notes" />
      </aside>
      <section class="edition" aria-labelledby="notes-title">
        <div class="ed-lead">
          <h1 id="notes-title">Public notes</h1>
        </div>
        <div class="ed-head">
          <a href="/slop/factory">Watch the factory</a>
        </div>
        {#if browser}
          <div class="graph">
            <GraphView focusId={page.url.searchParams.get("focus")} />
          </div>
        {/if}
      </section>
    </div>
  </div>
</main>

<style>
  .graph {
    height: 80dvh;
    min-height: 480px;
    min-width: 0;
  }
</style>
