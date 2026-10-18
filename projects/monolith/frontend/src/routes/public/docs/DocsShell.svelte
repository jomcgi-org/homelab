<script>
  import {
    SiteIndex,
    TechnicalDrawingChrome,
    Trail,
  } from "$lib/public/components";
  import "$lib/public/styles/technical-drawing.css";
  import DocsSearch from "./DocsSearch.svelte";

  /**
   * The docs sheet: trail, search, the page's own index and the project
   * index in the spine; the document, in an outlined panel, as the page's
   * content. The page owns the panel; this owns everything around it.
   * @type {{
   *   sidebar: {project:string,title:string,slug:string,tabs:{kind:string,label:string,slug:string,title:string}[]}[],
   *   toc?: { depth:number, text:string, id:string }[],
   *   activeSlug?: string,
   *   current?: string,
   *   children: import('svelte').Snippet,
   * }}
   */
  let { sidebar, toc = [], activeSlug = "", current = "", children } = $props();

  const hasToc = $derived(Array.isArray(toc) && toc.length > 0);
  const activeProject = $derived(
    sidebar.find((project) =>
      project.tabs.some((tab) => tab.slug === activeSlug),
    )?.project ?? "",
  );

  // Search indexes the public project, document kind, and document title. No
  // manifest bodies or legacy section metadata cross to the client.
  const allDocs = $derived(
    sidebar.flatMap((project) =>
      project.tabs.map((tab) => ({
        slug: tab.slug,
        title: tab.title,
        project: project.project,
        kind: tab.kind,
        label: tab.label,
      })),
    ),
  );

  // "5.1 Prefill was copying whole layers" -> ["5.1", "Prefill was ..."] so
  // the number sits in its own fixed column and the titles align. A document
  // with no numbered heading gets no column at all.
  function split(text) {
    const m = /^(\d+(?:\.\d+)*\.?)\s+(.*)$/.exec(text);
    return m ? [m[1], m[2]] : ["", text];
  }
  const entries = $derived(
    toc.map((heading) => {
      const [num, title] = split(heading.text);
      return { ...heading, num, title };
    }),
  );
  const numbered = $derived(entries.some((entry) => entry.num));

  // The index follows the reader: the last heading whose top has passed
  // the reading line at 30% of the viewport is the one being read.
  let activeId = $state("");

  $effect(() => {
    void toc;
    if (!hasToc) return;
    const headings = [
      ...document.querySelectorAll(".prose h2[id], .prose h3[id]"),
    ];
    if (!headings.length) return;
    const first = headings[0].id;
    const last = headings[headings.length - 1].id;
    const pick = () => {
      const atEnd =
        window.innerHeight + window.scrollY >=
        document.documentElement.scrollHeight - 2;
      if (atEnd) return last;
      if (window.scrollY === 0) return first;
      const line = window.innerHeight * 0.3;
      let best = null;
      for (const h of headings) {
        if (h.getBoundingClientRect().top <= line + 1) best = h.id;
      }
      return best ?? first;
    };
    const update = () => (activeId = pick());
    update();
    window.addEventListener("scroll", update, { passive: true });
    window.addEventListener("resize", update, { passive: true });
    return () => {
      window.removeEventListener("scroll", update);
      window.removeEventListener("resize", update);
    };
  });
</script>

{#snippet trail()}
  <Trail crumbs={[{ label: "docs", href: "/docs" }]} {current} />
{/snippet}

<TechnicalDrawingChrome {trail} />

<main class="td sheet docs-page">
  <div class="frame">
    <div class="journal">
      <aside class="spine">
        <div class="trail-dock">
          {@render trail()}
        </div>

        <DocsSearch docs={allDocs} />

        {#if hasToc}
          <nav class="index strip" aria-label="On this page">
            <p class="sec-label">/ Index</p>
            <div class="index-list">
              {#each entries as entry (entry.id)}
                <a
                  href={`#${entry.id}`}
                  class:sub={entry.depth === 3}
                  class:active={activeId === entry.id}
                  aria-current={activeId === entry.id ? "location" : undefined}
                >
                  {#if numbered}<span class="num">{entry.num}</span>{/if}
                  <span>{entry.title}</span>
                </a>
              {/each}
            </div>
          </nav>
        {/if}

        <nav class="index projects" aria-label="Projects">
          <p class="sec-label">/ Projects</p>
          {#each sidebar as project (project.project)}
            <details
              class="index-group"
              open={activeProject === project.project}
            >
              <summary>
                <span class="group-name">{project.title}</span>
                <span class="group-count">{project.tabs.length}</span>
              </summary>
              <div class="index-list">
                {#each project.tabs as tab (tab.kind)}
                  <a
                    href={`/docs/${tab.slug}`}
                    class:active={tab.slug === activeSlug}
                    aria-current={tab.slug === activeSlug ? "page" : undefined}
                    title={tab.title}>{tab.label}</a
                  >
                {/each}
              </div>
            </details>
          {/each}
        </nav>

        <SiteIndex here="docs" />
      </aside>

      <div class="content">
        {@render children()}
      </div>
    </div>
  </div>
</main>

<style>
  @media (prefers-reduced-motion: no-preference) {
    :global(html) {
      scroll-behavior: smooth;
    }
  }

  @media (max-width: 760px) {
    /* The trail's "docs" crumb is the way to the project list on a phone;
       the index strip keeps the page's own headings in reach. */
    .projects {
      display: none;
    }
  }
</style>
