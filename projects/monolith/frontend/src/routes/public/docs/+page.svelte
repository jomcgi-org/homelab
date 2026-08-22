<script>
  import { Seo } from "$lib/public/components";
  import DocsShell from "./DocsShell.svelte";
  import DocsTabs from "./DocsTabs.svelte";

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
      Current-state documentation for the public projects, rendered from the
      repository.
    </p>
  </header>

  <section class="docs-overview" aria-label="Public projects">
    {#each data.projects as project}
      <article class="project-card">
        <h2><a href={`/docs/${project.slug}`}>{project.title}</a></h2>
        <p class="project-name mono">{project.project}</p>
        <p class="project-excerpt">{project.excerpt}</p>
        <DocsTabs tabs={project.tabs} compact />
      </article>
    {/each}
  </section>
</DocsShell>

<style>
  .docs-hero {
    margin-bottom: 32px;
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

  .docs-overview {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 18px;
  }

  .project-card {
    display: flex;
    flex-direction: column;
    min-width: 0;
    padding: 20px;
    border: 2px solid var(--ink);
    background: var(--paper);
    box-shadow: var(--shadow-hard-sm);
  }

  .project-card h2 {
    padding: 0;
    margin: 0 0 2px;
    border: 0;
    font-family: var(--serif);
    font-size: 1.5em;
    font-weight: 400;
    letter-spacing: -0.01em;
  }

  .project-card h2 a {
    color: var(--ink);
    text-decoration: none;
  }

  .project-card h2 a:hover {
    text-decoration: underline;
    text-decoration-color: var(--coral);
    text-decoration-thickness: 2px;
    text-underline-offset: 3px;
  }

  .project-name {
    margin: 0 0 14px;
    color: var(--ink-3);
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }

  .project-excerpt {
    flex: 1 1 auto;
    margin: 0 0 18px;
    font-family: var(--sans);
    font-size: 0.92em;
    line-height: 1.55;
    color: var(--ink-2);
  }

  @media (max-width: 720px) {
    .docs-overview {
      grid-template-columns: minmax(0, 1fr);
    }
  }
</style>
