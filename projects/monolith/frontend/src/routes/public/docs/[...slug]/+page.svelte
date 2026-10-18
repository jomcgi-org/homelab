<script>
  import { Seo } from "$lib/public/components";
  import DocsShell from "../DocsShell.svelte";
  import DocsTabs from "../DocsTabs.svelte";

  let { data } = $props();

  let articleEl = $state();
  let mermaidApi = null;

  // Renders every unrendered `.doc-mermaid` block under articleEl to SVG. Runs
  // after mount and again whenever data.html changes (client-side navigation
  // between docs reuses this component, so the effect re-fires on the new
  // slug's content rather than remounting). $effect never runs during SSR, so
  // the mermaid import stays out of the server bundle and out of the initial
  // client bundle until a doc actually has a mermaid fence.
  $effect(() => {
    const html = data.html; // read so the effect re-runs when the doc changes
    if (!articleEl || !html) return;
    const blocks = articleEl.querySelectorAll("pre.doc-mermaid");
    if (blocks.length === 0) return;

    let cancelled = false;
    (async () => {
      if (!mermaidApi) {
        const mod = await import("mermaid");
        mermaidApi = mod.default;
        mermaidApi.initialize({ startOnLoad: false, theme: "neutral" });
      }
      if (cancelled) return;
      for (const pre of blocks) {
        const source = pre.textContent;
        try {
          const id = `mermaid-${Math.random().toString(36).slice(2)}`;
          const { svg } = await mermaidApi.render(id, source);
          if (cancelled) return;
          const wrapper = document.createElement("div");
          wrapper.className = "doc-mermaid-rendered";
          wrapper.innerHTML = svg;
          pre.replaceWith(wrapper);
        } catch {
          // Leave the source block untouched on any render error.
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  });
</script>

<Seo
  title={`${data.title} · Docs · jomcgi.dev`}
  description={data.meta.description}
  path={`/docs/${data.slug}`}
  type="article"
/>

<DocsShell
  sidebar={data.sidebar}
  toc={data.toc}
  activeSlug={data.slug}
  current={data.title}
>
  <article class="edition doc" aria-labelledby="doc-h">
    <div class="ed-head">
      <span>{data.project}</span>
      <span class="end">
        <DocsTabs tabs={data.tabs} activeKind={data.kind} />
      </span>
    </div>
    <div class="ed-lead">
      <h1 id="doc-h">{data.title}</h1>
    </div>
    <!-- Server-rendered, sanitised first-party markdown (raw HTML escaped by the
         renderer). The manifest never reaches the client; only this HTML does. -->
    <div class="ed-body prose" bind:this={articleEl}>{@html data.html}</div>
  </article>
</DocsShell>

<style>
  /* Client-rendered mermaid SVG, replacing a pre.doc-mermaid block. */
  .prose :global(.doc-mermaid-rendered) {
    display: block;
    max-width: 100%;
    margin: 1rem 0;
    overflow-x: auto;
    text-align: center;
  }

  .prose :global(.doc-mermaid-rendered svg) {
    max-width: 100%;
    height: auto;
  }
</style>
