<script>
  import { Footer } from "$lib/public/components";
  import DocsSearch from "./DocsSearch.svelte";

  /**
   * @type {{
   *   sidebar: { reference: {slug:string,title:string}[], decisions: { index: {slug:string,title:string}|null, categories: {name:string, items:{slug:string,title:string}[]}[] } },
   *   toc?: { depth:number, text:string, id:string }[],
   *   activeSlug?: string,
   *   children: import('svelte').Snippet,
   * }}
   */
  let { sidebar, toc = [], activeSlug = "", children } = $props();

  const hasToc = $derived(Array.isArray(toc) && toc.length > 0);

  // Top-nav active state, mirroring the old VitePress nav: ADRs lights up on any
  // decisions page, Architecture on any other (reference) doc. The /docs index
  // (empty slug) leaves both inactive.
  const onDecisions = $derived(activeSlug.startsWith("decisions"));
  const onReference = $derived(activeSlug !== "" && !onDecisions);

  // Flat doc list for search: titles/slugs only (already client-safe), tagged
  // with their sidebar group so results can show where each doc lives.
  const allDocs = $derived([
    ...sidebar.reference.map((d) => ({
      slug: d.slug,
      title: d.title,
      group: "Reference",
    })),
    ...(sidebar.decisions.index
      ? [
          {
            slug: sidebar.decisions.index.slug,
            title: sidebar.decisions.index.title,
            group: "Decisions",
          },
        ]
      : []),
    ...sidebar.decisions.categories.flatMap((c) =>
      c.items.map((d) => ({ slug: d.slug, title: d.title, group: c.name })),
    ),
  ]);

  // Collapsible ADR categories (accordion), matching VitePress's `collapsed`
  // groups. Start collapsed except the category holding the active doc, which
  // opens so the current page is visible on load.
  const DECISIONS_PREFIX = "decisions/";
  const activeCat = activeSlug.startsWith(DECISIONS_PREFIX)
    ? activeSlug.slice(DECISIONS_PREFIX.length).split("/")[0]
    : null;
  let openCats = $state(activeCat ? { [activeCat]: true } : {});

  /** @param {string} name */
  function toggleCat(name) {
    openCats = { ...openCats, [name]: !openCats[name] };
  }
</script>

<header class="docs-topbar">
  <div class="docs-topbar-inner">
    <a class="docs-back" href="https://jomcgi.dev/">
      <span class="docs-back-arrow" aria-hidden="true">&larr;</span>
      <span class="docs-back-label">jomcgi.dev</span>
    </a>

    <div class="docs-topbar-right">
      <DocsSearch docs={allDocs} />

      <nav class="docs-topnav" aria-label="Documentation sections">
        <a
          class="docs-topnav-link"
          class:active={onReference}
          href="/docs/services">Architecture</a
        >
        <a
          class="docs-topnav-link"
          class:active={onDecisions}
          href="/docs/decisions">ADRs</a
        >
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

<div class="docs-layout" class:has-toc={hasToc}>
  <aside class="docs-side">
    <nav class="side-nav mono" aria-label="Documentation">
      <p class="side-head">Reference</p>
      <ul class="side-list">
        {#each sidebar.reference as item}
          <li>
            <a
              class="side-link"
              class:active={activeSlug === item.slug}
              href={`/docs/${item.slug}`}>{item.title}</a
            >
          </li>
        {/each}
      </ul>

      <p class="side-head">Decisions</p>
      <ul class="side-list">
        {#if sidebar.decisions.index}
          <li>
            <a
              class="side-link"
              class:active={activeSlug === sidebar.decisions.index.slug}
              href={`/docs/${sidebar.decisions.index.slug}`}>Index</a
            >
          </li>
        {/if}
      </ul>

      {#each sidebar.decisions.categories as cat}
        <button
          type="button"
          class="side-cat"
          aria-expanded={!!openCats[cat.name]}
          onclick={() => toggleCat(cat.name)}
        >
          <svg
            class="side-cat-chevron"
            class:open={openCats[cat.name]}
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
          <span class="side-cat-name">{cat.name}</span>
          <span class="side-cat-count">{cat.items.length}</span>
        </button>
        {#if openCats[cat.name]}
          <ul class="side-list">
            {#each cat.items as item}
              <li>
                <a
                  class="side-link"
                  class:active={activeSlug === item.slug}
                  href={`/docs/${item.slug}`}
                  title={item.title}>{item.title}</a
                >
              </li>
            {/each}
          </ul>
        {/if}
      {/each}
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

<Footer />

<style>
  /* ── Top bar: apex back link on the left, docs section nav on the right.
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

  /* ── Right-hand cluster: search + section nav ── */
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
    border: 2px solid var(--ink);
    background: var(--paper);
    box-shadow: var(--shadow-hard-sm);
    padding: 16px 14px;
  }

  .side-head {
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--ink);
    background: var(--accent);
    display: inline-block;
    padding: 2px 8px;
    margin: 14px 0 8px;
    border: 2px solid var(--ink);
  }

  .side-head:first-child {
    margin-top: 0;
  }

  /* ── Collapsible ADR category (accordion disclosure) ── */
  .side-cat {
    display: flex;
    align-items: center;
    gap: 6px;
    width: 100%;
    margin: 12px 0 6px;
    padding: 4px 0;
    background: none;
    border: none;
    cursor: pointer;
    font-family: var(--mono);
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--ink-3);
    text-align: left;
    transition: color 120ms ease;
  }

  .side-cat:hover {
    color: var(--ink);
  }

  .side-cat-chevron {
    flex: 0 0 auto;
    transition: transform 140ms ease;
  }

  .side-cat-chevron.open {
    transform: rotate(90deg);
  }

  .side-cat-name {
    flex: 1 1 auto;
  }

  .side-cat-count {
    flex: 0 0 auto;
    color: var(--ink-3);
  }

  .side-list {
    list-style: none;
    margin: 0 0 4px;
    padding: 0;
  }

  .side-link {
    display: block;
    font-family: var(--mono);
    font-size: 12px;
    line-height: 1.35;
    color: var(--ink-2);
    text-decoration: none;
    padding: 5px 8px;
    border: 2px solid transparent;
    transition:
      border-color 120ms ease,
      background 120ms ease;
  }

  .side-link:hover {
    border-color: var(--ink);
    background: var(--bg-elev);
  }

  .side-link.active {
    border-color: var(--ink);
    background: var(--accent);
    font-weight: 600;
  }

  /* ── Main content card ───────────────────────────────────── */
  .docs-main {
    min-width: 0;
  }

  .docs-card {
    border: 2px solid var(--ink);
    background: var(--paper);
    box-shadow: var(--shadow-hard);
    padding: 40px 44px;
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
    background: var(--accent);
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
    box-shadow: var(--shadow-hard-sm);
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
    color: var(--ink);
    background: var(--accent);
    padding: 2px 7px;
    border-left: 2px solid var(--ink);
    border-bottom: 2px solid var(--ink);
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
    background: var(--accent);
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
