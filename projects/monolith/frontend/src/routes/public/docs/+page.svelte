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
  <section class="edition intro" aria-labelledby="docs-h">
    <div class="ed-head">
      <span
        ><span class="num">{data.projects.length}</span> public projects</span
      >
    </div>
    <div class="ed-lead">
      <h1 id="docs-h">{data.meta.title}</h1>
      <p>{data.meta.description}</p>
    </div>
  </section>

  <section class="editions ruled" aria-label="Public projects">
    {#each data.projects as project (project.project)}
      <article class="edition">
        <div class="ed-head">
          <span>{project.project}</span>
          <span class="end">
            <DocsTabs tabs={project.tabs} />
          </span>
        </div>
        <div class="ed-lead">
          <h2><a href={`/docs/${project.slug}`}>{project.title}</a></h2>
          {#if project.excerpt}
            <p>{project.excerpt}</p>
          {/if}
        </div>
      </article>
    {/each}
  </section>
</DocsShell>

<style>
  .intro {
    margin-bottom: 1.5rem;
  }
</style>
