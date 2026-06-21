<script>
  import { Seo } from "$lib/public/components";
  import DocsShell from "./DocsShell.svelte";

  let { data } = $props();
</script>

<Seo title="Documentation · jomcgi.dev" description={data.meta.description} path="/docs" />

<DocsShell sidebar={data.sidebar} activeSlug="">
  <header class="docs-hero">
    <p class="eyebrow">Homelab</p>
    <h1>Documentation</h1>
    <p class="lede">
      Reference docs and architecture decision records for the secure Kubernetes
      homelab: how the platform is built, why the decisions were made, and how
      the services fit together. Rendered straight from the repository.
    </p>
  </header>

  <section class="docs-overview">
    <div class="ov-group">
      <h2>Reference</h2>
      <ul class="ov-grid">
        {#each data.sidebar.reference as item}
          <li>
            <a class="ov-card" href={`/docs/${item.slug}`}>{item.title}</a>
          </li>
        {/each}
      </ul>
    </div>

    <div class="ov-group">
      <h2>Architecture Decisions</h2>
      {#if data.sidebar.decisions.index}
        <p class="ov-note">
          <a href={`/docs/${data.sidebar.decisions.index.slug}`}
            >Browse the full ADR index &rarr;</a
          >
        </p>
      {/if}
      <ul class="ov-cats">
        {#each data.sidebar.decisions.categories as cat}
          <li class="ov-cat">
            <span class="ov-cat-name">{cat.name}</span>
            <span class="ov-cat-count mono">{cat.items.length}</span>
          </li>
        {/each}
      </ul>
    </div>
  </section>
</DocsShell>

<style>
  .docs-hero {
    margin-bottom: 28px;
  }

  .docs-hero h1 {
    font-family: var(--serif);
    font-weight: 400;
    font-size: 3em;
    letter-spacing: -0.02em;
    line-height: 1;
    margin: 6px 0 14px;
  }

  .lede {
    font-family: var(--sans);
    font-size: 1.05em;
    line-height: 1.6;
    color: var(--ink-2);
    max-width: 64ch;
  }

  .ov-group + .ov-group {
    margin-top: 34px;
  }

  .ov-group h2 {
    font-family: var(--mono);
    font-size: 0.95em;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--ink);
    padding-bottom: 8px;
    border-bottom: 2px solid var(--ink);
    margin-bottom: 16px;
  }

  .ov-grid {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    gap: 12px;
  }

  .ov-card {
    display: block;
    font-family: var(--mono);
    font-size: 0.85em;
    font-weight: 600;
    color: var(--ink);
    text-decoration: none;
    padding: 14px 16px;
    border: 2px solid var(--ink);
    background: var(--paper);
    box-shadow: var(--shadow-hard-sm);
    transition:
      transform 120ms ease,
      box-shadow 120ms ease,
      background 120ms ease;
  }

  .ov-card:hover {
    transform: translate(-2px, -2px);
    box-shadow: var(--shadow-hard);
    background: var(--accent);
  }

  .ov-note {
    font-family: var(--mono);
    font-size: 0.85em;
    margin: 0 0 14px;
  }

  .ov-note a {
    color: var(--ink);
    text-decoration: underline;
    text-decoration-color: var(--coral);
    text-decoration-thickness: 2px;
    text-underline-offset: 2px;
  }

  .ov-cats {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
    gap: 10px;
  }

  .ov-cat {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 10px 14px;
    border: 2px solid var(--ink);
    background: var(--bg-elev);
  }

  .ov-cat-name {
    font-family: var(--mono);
    font-size: 0.82em;
    font-weight: 600;
    letter-spacing: 0.04em;
    text-transform: capitalize;
  }

  .ov-cat-count {
    font-size: 0.78em;
    font-weight: 700;
    color: var(--ink);
    background: var(--accent);
    border: 2px solid var(--ink);
    padding: 0 7px;
  }
</style>
