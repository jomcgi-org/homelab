<script>
  import { Seo } from "$lib/public/components";
  import DocsShell from "../DocsShell.svelte";

  let { data } = $props();

  const sectionLabel = $derived(
    data.section === "Decisions" ? "Architecture Decision Record" : "Reference",
  );
</script>

<Seo
  title={`${data.title} · Docs · jomcgi.dev`}
  description={data.meta.description}
  path={`/docs/${data.slug}`}
  type="article"
/>

<DocsShell sidebar={data.sidebar} toc={data.toc} activeSlug={data.slug}>
  <p class="doc-eyebrow mono">{sectionLabel}</p>
  <!-- Server-rendered, sanitised first-party markdown (raw HTML escaped by the
       renderer). The manifest never reaches the client; only this HTML does. -->
  <article class="doc-body">{@html data.html}</article>
</DocsShell>

<style>
  .doc-eyebrow {
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--ink);
    background: var(--accent);
    display: inline-block;
    padding: 3px 9px;
    border: 2px solid var(--ink);
    margin: 0 0 18px;
  }
</style>
