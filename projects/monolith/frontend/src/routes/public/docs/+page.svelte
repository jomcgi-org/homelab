<script>
  import { Seo } from "$lib/public/components";
  import DocsShell from "./DocsShell.svelte";

  let { data } = $props();
</script>

<Seo
  title="Documentation · jomcgi.dev"
  description={data.meta.description}
  path="/docs"
/>

<DocsShell sidebar={data.sidebar} activeSlug="">
  <header class="docs-hero">
    <p class="eyebrow">Homelab</p>
    <h1>Documentation</h1>
    <p class="lede">
      Project READMEs and architecture decision records for the secure
      Kubernetes homelab: how the platform is built, why the decisions were
      made, and how the services fit together. Rendered straight from the
      repository.
    </p>
  </header>

  <section class="docs-overview">
    <h2>Projects</h2>
    <ul class="ov-list">
      {#each data.sidebar.projects as node}
        <li>
          {#if node.slug}
            <a class="ov-link" href={`/docs/${node.slug}`}>
              <span class="ov-name">{node.name}</span>
              {#if node.title !== node.name}
                <span class="ov-title">{node.title}</span>
              {/if}
            </a>
          {:else}
            <span class="ov-name ov-name-muted">{node.name}</span>
          {/if}
        </li>
      {/each}
    </ul>
  </section>
</DocsShell>

<style>
  .docs-hero {
    margin-bottom: 36px;
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

  .docs-overview h2 {
    font-family: var(--mono);
    font-size: 0.95em;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--ink);
    padding-bottom: 8px;
    border-bottom: 2px solid var(--ink);
    margin-bottom: 4px;
  }

  .ov-list {
    list-style: none;
    margin: 0;
    padding: 0;
  }

  .ov-list li + li {
    border-top: 1px solid var(--rule-2);
  }

  .ov-link {
    display: flex;
    align-items: baseline;
    gap: 16px;
    padding: 12px 4px;
    text-decoration: none;
    color: var(--ink);
  }

  .ov-link:hover .ov-name {
    text-decoration: underline;
    text-decoration-color: var(--coral);
    text-decoration-thickness: 2px;
    text-underline-offset: 3px;
  }

  .ov-name {
    font-family: var(--mono);
    font-size: 0.9em;
    font-weight: 600;
  }

  .ov-name-muted {
    display: block;
    padding: 12px 4px;
    color: var(--ink-2);
  }

  .ov-title {
    font-family: var(--sans);
    font-size: 0.9em;
    color: var(--ink-2);
  }
</style>
