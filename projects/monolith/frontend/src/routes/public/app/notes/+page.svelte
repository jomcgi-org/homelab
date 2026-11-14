<script>
  import { onMount } from "svelte";
  import { page } from "$app/state";
  import {
    SiteIndex,
    TechnicalDrawingChrome,
    Trail,
  } from "$lib/public/components";
  import "$lib/public/styles/technical-drawing.css";

  let { data = {} } = $props();
  let GraphView = $state(null);
  let graphStatus = $state("loading");
  let attempt = $state(0);
  let now = $state(Date.now());
  let stopped = false;
  const overview = $derived(
    data.overview?.expiresAt > now ? data.overview : null,
  );

  $effect(() => {
    const deadline = data.overview?.expiresAt;
    const current = Date.now();
    now = current;
    if (!deadline) return;
    const timer = setTimeout(
      () => (now = Date.now()),
      Math.max(0, deadline - current),
    );
    return () => clearTimeout(timer);
  });

  async function loadGraph() {
    graphStatus = "loading";
    try {
      const module = await import("$lib/public/notes/GraphView.svelte");
      if (!stopped) {
        GraphView = module.default;
        attempt++;
      }
    } catch {
      if (!stopped) graphStatus = "error";
    }
  }

  onMount(() => {
    // Let the server-rendered titles paint before downloading the canvas,
    // markdown renderer and D3. The full graph keeps its own HTTP lease.
    let frame = requestAnimationFrame(() => {
      frame = requestAnimationFrame(loadGraph);
    });
    return () => {
      stopped = true;
      cancelAnimationFrame(frame);
    };
  });
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
        <div class="graph">
          <div
            class="graph-overview"
            hidden={graphStatus === "painted"}
            aria-label="Public notes overview"
          >
            {#if overview}
              <p class="graph-counts">
                <span>{overview.nodeCount} notes</span>
                <span>{overview.edgeCount} links</span>
              </p>
              {#if overview.titles.length}
                <p class="sec-label">/ Connected notes</p>
                <ul>
                  {#each overview.titles as note (note.id)}
                    <li>
                      <a href={`?focus=${encodeURIComponent(note.id)}`}
                        >{note.title}</a
                      >
                    </li>
                  {/each}
                </ul>
              {:else}
                <p>No current public notes.</p>
              {/if}
            {:else}
              <p>No current snapshot is available.</p>
            {/if}
            {#if graphStatus === "error"}
              <p>The graph is unavailable right now.</p>
              <button type="button" onclick={loadGraph}>TRY AGAIN</button>
            {/if}
          </div>
          {#if GraphView}
            <div
              class="interactive-graph"
              class:ready={graphStatus === "painted"}
              inert={graphStatus !== "painted"}
            >
              {#key attempt}
                <GraphView
                  focusId={page.url.searchParams.get("focus")}
                  onStatus={(status) => (graphStatus = status)}
                />
              {/key}
            </div>
          {/if}
        </div>
      </section>
    </div>
  </div>
</main>

<style>
  .graph {
    position: relative;
    height: 80dvh;
    min-height: 480px;
    min-width: 0;
  }
  .graph-overview,
  .interactive-graph {
    position: absolute;
    inset: 0;
  }
  .graph-overview {
    overflow-y: auto;
    padding: 12px 16px;
  }
  .graph-counts {
    display: flex;
    gap: 24px;
    font-family: var(--font-code);
  }
  .graph-overview ul {
    padding: 0;
    list-style: none;
  }
  .graph-overview li {
    border-bottom: 1px solid var(--line);
  }
  .graph-overview a {
    display: block;
    padding: 12px 0;
    overflow-wrap: anywhere;
  }
  .graph-overview button {
    min-height: 44px;
    padding: 8px 12px;
    font: inherit;
    color: var(--ink);
    background: var(--band);
    border: 1px solid var(--stroke);
  }
  .graph-overview :is(a, button):focus-visible {
    outline: 2px solid var(--accent-ink);
    outline-offset: 2px;
  }
  .interactive-graph {
    visibility: hidden;
  }
  .interactive-graph.ready {
    visibility: visible;
  }
</style>
