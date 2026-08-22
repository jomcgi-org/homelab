<script>
  import DocsSearch from "./DocsSearch.svelte";
  import DocsTabs from "./DocsTabs.svelte";

  /**
   * @type {{
   *   sidebar: {project:string,title:string,slug:string,tabs:{kind:string,label:string,slug:string,title:string}[]}[],
   *   toc?: { depth:number, text:string, id:string }[],
   *   activeSlug?: string,
   *   children: import('svelte').Snippet,
   * }}
   */
  let { sidebar, toc = [], activeSlug = "", children } = $props();

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

  let openProject = $state("");

  /** @param {string} project */
  function toggleProject(project) {
    openProject = openProject === project ? "" : project;
  }
</script>

<header class="docs-topbar">
  <div class="docs-topbar-inner">
    <a class="docs-back" href="/">
      <span class="docs-back-arrow" aria-hidden="true">&larr;</span>
      <span class="docs-back-label">jomcgi.dev</span>
    </a>

    <div class="docs-topbar-right">
      <DocsSearch docs={allDocs} />

      <nav class="docs-topnav" aria-label="Documentation links">
        <a
          class="docs-topnav-link"
          href="https://github.com/jomcgi/homelab"
          target="_blank"
          rel="noopener noreferrer">GitHub</a
        >
      </nav>
    </div>
  </div>
</header>

<div class="docs-page">
  <div class="docs-layout" class:has-toc={hasToc}>
    <aside class="docs-side">
      <nav class="side-nav mono" aria-label="Documentation">
        <p class="side-head">Projects</p>
        <ul class="side-list">
          {#each sidebar as project}
            {@const expanded =
              activeProject === project.project ||
              openProject === project.project}
            <li class="side-project" class:expanded>
              <div class="side-project-row">
                <a
                  class="side-project-link"
                  class:active={activeProject === project.project}
                  href={`/docs/${project.slug}`}
                  title={project.title}>{project.title}</a
                >
                <button
                  type="button"
                  class="side-project-toggle"
                  aria-label={`Toggle ${project.title} document tabs`}
                  aria-expanded={expanded}
                  onclick={() => toggleProject(project.project)}
                >
                  <svg
                    class="side-project-chevron"
                    class:open={expanded}
                    width="9"
                    height="9"
                    viewBox="0 0 10 10"
                    aria-hidden="true"
                  >
                    <path
                      d="M3 1 L7 5 L3 9"
                      fill="none"
                      stroke="currentColor"
                      stroke-width="1.6"
                      stroke-linecap="square"
                    />
                  </svg>
                </button>
              </div>
              <div class="side-project-tabs">
                <DocsTabs
                  tabs={project.tabs}
                  activeKind={activeProject === project.project
                    ? project.tabs.find((tab) => tab.slug === activeSlug)?.kind
                    : ""}
                  compact
                />
              </div>
            </li>
          {/each}
        </ul>
      </nav>
    </aside>

    <main class="docs-main">
      <div class="docs-card">
        {@render children()}
      </div>
    </main>

    {#if hasToc}
      <aside class="docs-toc mono" aria-label="On this page">
        <p class="side-head">On this page</p>
        <ul class="toc-list">
          {#each toc as h}
            <li class:lvl3={h.depth === 3}>
              <a href={`#${h.id}`}>{h.text}</a>
            </li>
          {/each}
        </ul>
      </aside>
    {/if}
  </div>
</div>

<style>
  /* ── Top bar: apex back link on the left, docs search and GitHub on the right.
     This is the *only* header on docs pages: the global site nav is
     suppressed on /docs (see routes/+layout.svelte), so this bar owns
     `top: 0` alone. Its height (~64px with the search box) sets the
     sidebar / TOC `top: 76px` offsets and heading scroll-margins. ── */
  .docs-topbar {
    position: sticky;
    top: 0;
    z-index: 50;
    background: var(--paper);
    border-bottom: 2px solid var(--ink);
  }

  .docs-topbar-inner {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    max-width: 1320px;
    margin: 0 auto;
    padding: 14px 32px;
  }

  .docs-back {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    font-family: var(--mono);
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--ink-2);
    text-decoration: none;
    transition: color 160ms ease;
  }

  .docs-back-arrow {
    font-size: 14px;
    transition: transform 160ms ease;
  }

  .docs-back:hover {
    color: var(--ink);
  }

  .docs-back:hover .docs-back-arrow {
    transform: translateX(-3px);
  }

  .docs-back-label {
    border-bottom: 2px solid transparent;
    transition: border-color 160ms ease;
  }

  .docs-back:hover .docs-back-label {
    border-bottom-color: var(--coral);
  }

  /* ── Right-hand cluster: search + repository link ── */
  .docs-topbar-right {
    display: flex;
    align-items: center;
    gap: 14px;
    min-width: 0;
  }

  .docs-topnav {
    display: flex;
    align-items: center;
    gap: 4px;
  }

  .docs-topnav-link {
    position: relative;
    padding: 6px 10px;
    font-family: var(--mono);
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--ink-2);
    text-decoration: none;
    transition: color 160ms ease;
  }

  .docs-topnav-link::after {
    content: "";
    position: absolute;
    left: 10px;
    right: 10px;
    bottom: 0;
    height: 2px;
    background: var(--coral);
    transform: scaleX(0);
    transition: transform 160ms ease;
  }

  .docs-topnav-link:hover {
    color: var(--ink);
  }

  .docs-topnav-link:hover::after {
    transform: scaleX(1);
  }

  .docs-topnav-link.active {
    color: var(--ink);
  }

  .docs-topnav-link.active::after {
    background: var(--ink);
    transform: scaleX(1);
  }

  @media (max-width: 1080px) {
    .docs-topbar-inner {
      padding: 12px 20px;
    }
  }

  @media (max-width: 560px) {
    .docs-topbar-inner {
      gap: 10px;
    }
    .docs-topnav {
      gap: 0;
    }
    .docs-topnav-link {
      padding: 6px 7px;
      font-size: 11px;
      letter-spacing: 0.06em;
    }
  }

  .docs-page {
    background: var(--paper);
    min-height: calc(100vh - 64px);
  }

  .docs-layout {
    display: grid;
    grid-template-columns: 240px minmax(0, 1fr);
    gap: 32px;
    max-width: 1320px;
    margin: 0 auto;
    padding: 32px;
    align-items: start;
  }

  .docs-layout.has-toc {
    grid-template-columns: 240px minmax(0, 1fr) 200px;
  }

  /* ── Left sidebar ────────────────────────────────────────── */
  .docs-side {
    position: sticky;
    top: 76px;
    max-height: calc(100vh - 96px);
    overflow-y: auto;
    padding: 0 14px 0 0;
  }

  .side-head {
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--ink);
    display: block;
    padding-bottom: 6px;
    margin: 18px 0 8px;
    border-bottom: 2px solid var(--ink);
  }

  .side-head:first-child {
    margin-top: 0;
  }

  .side-list {
    list-style: none;
    margin: 0 0 4px;
    padding: 0;
  }

  .side-project {
    padding: 4px 0;
  }

  .side-project + .side-project {
    border-top: 1px solid var(--rule-2);
  }

  .side-project-row {
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .side-project-link {
    flex: 1 1 auto;
    min-width: 0;
    padding: 6px 4px;
    font-family: var(--mono);
    font-size: 12px;
    font-weight: 600;
    line-height: 1.3;
    color: var(--ink-2);
    text-decoration: none;
  }

  .side-project-link:hover,
  .side-project-link.active {
    color: var(--ink);
  }

  .side-project-link.active {
    text-decoration: underline;
    text-decoration-color: var(--coral);
    text-decoration-thickness: 2px;
    text-underline-offset: 3px;
  }

  .side-project-toggle {
    display: grid;
    place-items: center;
    flex: 0 0 auto;
    padding: 6px;
    background: none;
    border: none;
    cursor: pointer;
    color: var(--ink-3);
    transition: color 120ms ease;
  }

  .side-project-toggle:hover {
    color: var(--ink);
  }

  .side-project-chevron {
    transition: transform 140ms ease;
  }

  .side-project-chevron.open {
    transform: rotate(90deg);
  }

  .side-project-tabs {
    display: none;
    padding: 0 2px 4px;
  }

  .side-project:hover .side-project-tabs,
  .side-project:focus-within .side-project-tabs,
  .side-project.expanded .side-project-tabs {
    display: block;
  }

  /* ── Main content card ───────────────────────────────────── */
  .docs-main {
    min-width: 0;
  }

  .docs-card {
    padding: 8px 0 0;
    /* Pin a fixed 16px base so the doc typography (expressed in `em`
       below) is immune to the fluid root font-size in global.css
       (`html { font-size: clamp(16px, max(1.6vw, 2.6vh), 48px) }`).
       `rem` here would resolve against that fluid root and blow the
       prose up ~46% on desktop; `em` resolves against this 16px. */
    font-size: 16px;
  }

  /* ── Right TOC ───────────────────────────────────────────── */
  .docs-toc {
    position: sticky;
    top: 76px;
    max-height: calc(100vh - 96px);
    overflow-y: auto;
    padding: 8px 0;
  }

  .toc-list {
    list-style: none;
    margin: 0;
    padding: 0;
    border-left: 2px solid var(--rule-2);
  }

  .toc-list li {
    margin: 0;
  }

  .toc-list li.lvl3 {
    padding-left: 14px;
  }

  .toc-list a {
    display: block;
    font-family: var(--mono);
    font-size: 11px;
    line-height: 1.3;
    color: var(--ink-3);
    text-decoration: none;
    padding: 5px 12px;
    margin-left: -2px;
    border-left: 2px solid transparent;
    transition:
      color 120ms ease,
      border-color 120ms ease;
  }

  .toc-list a:hover {
    color: var(--ink);
    border-left-color: var(--coral);
  }

  /* ── Responsive: shed the rails in two stages ─────────────────
     The right TOC is the first to go (≤1200px): below that width the
     nav + content + TOC triple squeezes the prose into a cramped
     column and makes the serif headings look oversized. Dropping the
     TOC first hands that space back to the content. The left nav only
     unsticks and stacks on top once the viewport can no longer hold a
     two-column layout (≤920px). */
  @media (max-width: 1200px) {
    .docs-layout.has-toc {
      grid-template-columns: 240px minmax(0, 1fr);
    }
    .docs-toc {
      display: none;
    }
  }

  @media (max-width: 920px) {
    .docs-layout,
    .docs-layout.has-toc {
      grid-template-columns: minmax(0, 1fr);
      gap: 20px;
      padding: 20px;
    }
    .docs-side {
      position: static;
      max-height: none;
    }
    .docs-card {
      padding: 28px 22px;
    }
  }

  /* ── Rendered doc body (server HTML via {@html}; needs :global) ──
     The markdown renderer emits plain semantic tags + <pre class="doc-code">,
     so these target the rendered output rather than scoped classes. */
  .docs-card :global(h1),
  .docs-card :global(h2),
  .docs-card :global(h3),
  .docs-card :global(h4) {
    font-family: var(--serif);
    font-weight: 400;
    line-height: 1.1;
    color: var(--ink);
    scroll-margin-top: 84px;
  }

  .docs-card :global(h1) {
    font-size: 2.2em;
    letter-spacing: -0.01em;
    margin: 0 0 20px;
  }

  .docs-card :global(h2) {
    font-size: 1.5em;
    margin: 34px 0 14px;
    padding-bottom: 8px;
    border-bottom: 2px solid var(--ink);
  }

  .docs-card :global(h3) {
    font-size: 1.2em;
    margin: 26px 0 10px;
  }

  .docs-card :global(h4) {
    font-family: var(--mono);
    font-size: 1em;
    font-weight: 600;
    margin: 22px 0 8px;
  }

  .docs-card :global(p),
  .docs-card :global(li) {
    font-family: var(--sans);
    font-size: 0.98em;
    line-height: 1.65;
    color: var(--ink-2);
  }

  .docs-card :global(p) {
    margin: 0 0 16px;
  }

  .docs-card :global(ul),
  .docs-card :global(ol) {
    margin: 0 0 16px;
    padding-left: 24px;
  }

  .docs-card :global(ul) {
    list-style: square;
  }

  .docs-card :global(ol) {
    list-style: decimal;
  }

  .docs-card :global(li) {
    margin: 4px 0;
  }

  .docs-card :global(a) {
    color: var(--ink);
    text-decoration: underline;
    text-decoration-color: var(--coral);
    text-underline-offset: 2px;
    text-decoration-thickness: 2px;
  }

  .docs-card :global(a:hover) {
    text-decoration-thickness: 3px;
  }

  .docs-card :global(strong) {
    font-weight: 700;
    color: var(--ink);
  }

  .docs-card :global(blockquote) {
    margin: 0 0 16px;
    padding: 4px 16px;
    border-left: 4px solid var(--accent);
    background: var(--bg-elev);
    color: var(--ink-2);
  }

  .docs-card :global(hr) {
    border: none;
    border-top: 2px dashed var(--rule-2);
    margin: 28px 0;
  }

  /* Inline code */
  .docs-card :global(code) {
    font-family: var(--mono);
    font-size: 0.85em;
    background: var(--bg-elev);
    border: 1px solid var(--rule-2);
    border-radius: 3px;
    padding: 1px 5px;
  }

  /* Fenced code blocks (pre.doc-code from the renderer) */
  .docs-card :global(pre.doc-code) {
    position: relative;
    font-family: var(--mono);
    font-size: 0.82em;
    line-height: 1.5;
    background: var(--ink);
    color: var(--cream); /* cream on ink: brutalist code panel */
    border: 2px solid var(--ink);
    border-radius: var(--radius);
    padding: 18px 18px 16px;
    margin: 0 0 20px;
    overflow-x: auto;
  }

  .docs-card :global(pre.doc-code code) {
    background: none;
    border: none;
    padding: 0;
    font-size: inherit;
    color: inherit;
  }

  /* Language label tab for fenced blocks (mermaid included) */
  .docs-card :global(pre.doc-code[data-lang])::before {
    content: attr(data-lang);
    position: absolute;
    top: 0;
    right: 0;
    font-family: var(--mono);
    font-size: 9px;
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--cream);
    padding: 2px 7px;
    border-left: 1px solid var(--rule-2);
    border-bottom: 1px solid var(--rule-2);
  }

  /* Tables */
  .docs-card :global(table) {
    width: 100%;
    border-collapse: collapse;
    margin: 0 0 22px;
    font-family: var(--mono);
    font-size: 0.8em;
    border: 2px solid var(--ink);
  }

  .docs-card :global(th),
  .docs-card :global(td) {
    border: 1px solid var(--rule-2);
    padding: 8px 10px;
    text-align: left;
    vertical-align: top;
    /* Long unbroken mono tokens (file paths like
       projects/platform/agent-sandbox, URLs) would otherwise force the
       auto-laid-out table wider than the card and bleed past its right
       border. Let them wrap inside the cell instead. */
    overflow-wrap: anywhere;
    word-break: break-word;
  }

  .docs-card :global(thead th) {
    background: var(--bg-elev);
    color: var(--ink);
    font-weight: 700;
    border-color: var(--ink);
  }

  .docs-card :global(tbody tr:nth-child(even)) {
    background: var(--bg-elev);
  }

  .docs-card :global(img) {
    max-width: 100%;
    border: 2px solid var(--ink);
  }
</style>
