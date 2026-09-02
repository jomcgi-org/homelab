<script>
  import { Seo } from "$lib/public/components";
  import { formatDate } from "../blog.js";

  let { data } = $props();
  let activeId = $state("");

  // The spine follows the reader: the topmost heading in the upper band of
  // the viewport is the active one, and the URL hash follows it so a copied
  // link lands on the section being read.
  $effect(() => {
    const headings = document.querySelectorAll(
      ".post-frame h2[id], .post-frame h3[id]",
    );
    if (!headings.length) return;
    const observer = new IntersectionObserver(
      (observed) => {
        const visible = observed
          .filter((item) => item.isIntersecting)
          .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
        if (!visible[0]) return;
        const id = visible[0].target.id;
        if (id === activeId) return;
        activeId = id;
        history.replaceState(null, "", `#${id}`);
      },
      { rootMargin: "-10% 0px -70% 0px" },
    );
    headings.forEach((h) => observer.observe(h));
    return () => observer.disconnect();
  });
</script>

<Seo
  title={`${data.title} · jomcgi.dev`}
  description={data.summary}
  path={`/blog/${data.slug}`}
  type="article"
/>

<main class="td post-page">
  <div class="frame">
    <div class="journal">
      <aside class="spine">
        {#if data.toc.length}
          <nav class="toc" aria-label="Sections">
            <p class="sec-label">/ Index</p>
            <ol>
              {#each data.toc as section}
                <li>
                  <a
                    href={`#${section.id}`}
                    class:active={activeId === section.id}
                    aria-current={activeId === section.id
                      ? "location"
                      : undefined}>{section.text}</a
                  >
                  {#if section.children.length}
                    <ol>
                      {#each section.children as sub}
                        <li>
                          <a
                            href={`#${sub.id}`}
                            class:active={activeId === sub.id}
                            aria-current={activeId === sub.id
                              ? "location"
                              : undefined}>{sub.text}</a
                          >
                        </li>
                      {/each}
                    </ol>
                  {/if}
                </li>
              {/each}
            </ol>
          </nav>
        {/if}
      </aside>

      <div class="post-frame">
        <article class="edition">
          <p class="ed-head">
            <time datetime={data.date}>{formatDate(data.date)}</time>
            <a href="/blog">Index</a>
          </p>

          <header class="ed-lead">
            <h1>{data.title}</h1>
            <p>{data.summary}</p>
          </header>

          {#if data.preamble}
            <!-- Server-rendered, constrained first-party markdown. -->
            <div class="post-body">{@html data.preamble}</div>
          {/if}
        </article>

        <!-- One panel per numbered section: a page flip between them. -->
        {#each data.sections as section}
          <section class="edition post-body">{@html section}</section>
        {/each}
      </div>
    </div>
  </div>
</main>

<style>
  .post-page {
    box-sizing: border-box;
    min-height: 100vh;
    padding: clamp(3.25rem, 5vh, 3.75rem) clamp(1.35em, 5vw, 4.5em) 6em;
    background: var(--sheet);
    color: var(--ink);
    font-family: var(--font-ui);
    font-size: 1rem;
  }

  .post-page a:focus-visible {
    outline: 2px solid var(--accent-ink);
    outline-offset: 3px;
  }

  .frame {
    max-width: 75em;
    margin: 0 auto;
  }

  .journal {
    display: grid;
    grid-template-columns: minmax(12em, 15em) minmax(0, 54em);
    gap: clamp(2em, 4vw, 4em);
  }

  .spine {
    position: sticky;
    top: 1.5rem;
    align-self: start;
    max-height: calc(100vh - 3rem);
    padding-left: 0.4rem;
    overflow-y: auto;
    scrollbar-width: none;
  }

  .sec-label {
    margin: 0 0 1em;
    padding-bottom: 0.55em;
    border-bottom: 1px solid var(--stroke);
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.68rem;
    font-weight: 500;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .toc ol {
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .toc a {
    display: block;
    padding: 0.34em 0 0.34em 0.6rem;
    border-left: 2px solid transparent;
    color: var(--ink-2);
    font-size: 0.8em;
    line-height: 1.3;
    text-decoration: none;
  }

  .toc ol ol a {
    padding-left: 1.4rem;
    font-size: 0.74em;
  }

  .toc a:hover,
  .toc a.active {
    color: var(--accent-ink);
  }

  .toc a.active {
    border-left-color: var(--accent-ink);
    font-weight: 650;
  }

  .post-frame {
    max-width: 54em;
    min-width: 0;
  }

  .edition {
    border: 1px solid var(--ink);
  }

  .edition + .edition {
    margin-top: 1.5rem;
  }

  .ed-head {
    display: flex;
    justify-content: space-between;
    margin: 0;
    padding: 0.55em 1rem;
    border-bottom: 1px solid var(--stroke);
    background: var(--band);
    font-family: var(--font-code);
    font-size: 0.72rem;
  }

  .ed-head time,
  .ed-head a {
    color: var(--ink-2);
  }

  .ed-head a {
    text-decoration: none;
  }

  .ed-head a:hover {
    color: var(--accent-ink);
  }

  .ed-lead {
    padding: 1rem;
  }

  .ed-lead h1 {
    max-width: 40ch;
    margin: 0;
    font-size: clamp(1.25rem, 2.5vw, 1.6rem);
    font-weight: 700;
    letter-spacing: -0.025em;
    line-height: 1.15;
  }

  .ed-lead p {
    margin: 0.6rem 0 0;
    color: var(--ink);
    font-size: 0.92rem;
    line-height: 1.55;
  }

  .post-body {
    padding: 0 1rem 1rem;
    counter-reset: fig;
  }

  .ed-lead + .post-body {
    border-top: 1px solid var(--stroke);
  }

  /* The panel body counts figures across the whole post, not per panel. */
  .post-frame {
    counter-reset: fig;
  }

  .post-body {
    counter-reset: none;
  }

  .post-body :global(h1),
  .post-body :global(h2),
  .post-body :global(h3),
  .post-body :global(h4),
  .post-body :global(h5),
  .post-body :global(h6) {
    color: var(--ink);
    scroll-margin-top: 4rem;
  }

  .post-body :global(h1),
  .post-body :global(h2) {
    margin: 1.4rem -1rem 0.85rem;
    padding: 0.45em 1rem;
    border-top: 1px solid var(--stroke);
    border-bottom: 1px solid var(--stroke);
    background: var(--band);
    font-family: var(--font-ui);
    font-size: 1.1rem;
    font-weight: 700;
    letter-spacing: -0.015em;
    line-height: 1.2;
  }

  /* The body's top rule already closes the lead, so a section band that
     opens the body sits directly on it. */
  .post-body :global(> h2:first-child) {
    margin-top: 0;
    border-top: 0;
  }

  .post-body :global(> :last-child) {
    margin-bottom: 0;
  }

  /* A table, figure, or code block that ends a panel ends on the panel's
     own border: no strip of sheet between its last rule and the outline. */
  .post-body :global(> table:last-child),
  .post-body :global(> figure.fig:last-child),
  .post-body :global(> pre.doc-code:last-child) {
    margin-bottom: -1rem;
  }

  .post-body :global(> table:last-child tr:last-child td),
  .post-body :global(> figure.fig:last-child) {
    border-bottom: 0;
  }

  /* A subsection is a partition of its section panel: a hairline rule edge
     to edge above the heading, the same way a part is divided inside its
     outline in the figures. */
  .post-body :global(h3) {
    margin: 1.6rem -1rem 0.55rem;
    padding: 1rem 1rem 0;
    border-top: 1px solid var(--line);
    font-family: var(--font-ui);
    font-size: 0.95rem;
    font-weight: 700;
    line-height: 1.25;
  }

  .post-body :global(h4),
  .post-body :global(h5),
  .post-body :global(h6) {
    margin: 1.2rem 0 0.45rem;
    font-family: var(--font-code);
    font-size: 0.82rem;
    font-weight: 600;
    line-height: 1.3;
  }

  .post-body :global(p) {
    margin: 0.85rem 0;
    color: var(--ink);
    font-size: 0.92rem;
    line-height: 1.6;
  }

  .post-body :global(a) {
    color: var(--accent-ink);
    text-decoration: underline;
    text-underline-offset: 0.18em;
  }

  .post-body :global(strong) {
    font-weight: 650;
  }

  /* The public tier's reset strips list markers; the post body wants them.
     Square markers, drawn in ink like everything else on the sheet. */
  .post-body :global(ul),
  .post-body :global(ol) {
    margin: 0.85rem 0;
    padding-left: 1.5rem;
  }

  .post-body :global(ul) {
    list-style: square;
  }

  .post-body :global(ol) {
    list-style: decimal;
  }

  .post-body :global(li::marker) {
    color: var(--ink-2);
  }

  .post-body :global(li) {
    margin: 0.25rem 0;
    color: var(--ink);
    font-size: 0.92rem;
    line-height: 1.55;
  }

  .post-body :global(blockquote) {
    margin: 1rem 0;
    padding-left: 1rem;
    border-left: 2px solid var(--stroke);
    color: var(--ink-2);
  }

  .post-body :global(blockquote p) {
    color: inherit;
  }

  .post-body :global(hr) {
    margin: 1.5rem 0;
    border: 0;
    border-top: 1px solid var(--stroke);
  }

  .post-body :global(code) {
    padding: 0.12em 0.3em;
    background: var(--band);
    font-family: var(--font-code);
    font-size: 0.85em;
  }

  /* Tables, figures, and code blocks run to the panel's edges so their rules
     join the outline: boxes made of lines inside the boundary, never a box
     inside a box. */
  .post-body :global(pre.doc-code) {
    margin: 1rem -1rem;
    padding: 1rem;
    overflow-x: auto;
    border-top: 1px solid var(--stroke);
    border-bottom: 1px solid var(--stroke);
    background: var(--band);
    color: var(--ink);
    font-family: var(--font-code);
    font-size: 0.8rem;
    line-height: 1.55;
  }

  .post-body :global(pre.doc-code code) {
    padding: 0;
    background: none;
    color: inherit;
    font-size: inherit;
  }

  .post-body :global(table) {
    width: calc(100% + 2rem);
    margin: 1rem -1rem;
    border-collapse: collapse;
  }

  .post-body :global(th) {
    padding: 0.5em 1rem;
    border-top: 1px solid var(--stroke);
    border-bottom: 1px solid var(--line);
    background: var(--band);
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.72rem;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-align: left;
    text-transform: uppercase;
  }

  .post-body :global(td) {
    padding: 0.55em 1rem;
    border-bottom: 1px solid var(--line);
    color: var(--ink);
    font-size: 0.85rem;
    line-height: 1.45;
    text-align: left;
    vertical-align: top;
  }

  .post-body :global(th + th),
  .post-body :global(td + td) {
    border-left: 1px solid var(--line);
  }

  .post-body :global(img) {
    display: block;
    max-width: 100%;
    height: auto;
  }

  .post-body :global(.fig) {
    margin: 1.5rem -1rem;
    border-top: 1px solid var(--stroke);
    border-bottom: 1px solid var(--stroke);
    counter-increment: fig;
  }

  .post-body :global(.fig-art) {
    padding: 1rem;
    overflow-x: auto;
    color: var(--ink);
  }

  /* A keyed figure is drawn for a 48em measure. Below that it scrolls
     sideways inside its panel instead of shrinking its 11px labels
     past legibility. */
  .post-body :global(.fig-art svg) {
    display: block;
    width: 100%;
    min-width: 36em;
    height: auto;
  }

  .post-body :global(.fig figcaption) {
    padding: 0.45em 1rem;
    border-top: 1px solid var(--stroke);
    background: var(--band);
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.72rem;
  }

  .post-body :global(.fig figcaption::before) {
    content: "Fig. " counter(fig) "  ";
  }

  .post-body :global(figure.fig + table) {
    margin-top: calc(-1.5rem - 1px);
  }

  /* A table or figure that opens a section docks to the band: the band's
     bottom rule is the table's top rule. */
  .post-body :global(h2 + table),
  .post-body :global(h2 + figure.fig) {
    margin-top: calc(-0.85rem - 1px);
  }

  .post-body :global(h2 + table th) {
    background: none;
  }

  .post-body :global(tr:last-child td) {
    border-bottom: 1px solid var(--stroke);
  }

  .post-body :global(figure.fig + table th:first-child),
  .post-body :global(figure.fig + table td:first-child) {
    width: 3.5em;
    font-family: var(--font-code);
    text-align: right;
  }

  /* The key column carries the part's tier colour as the cell itself: the
     colour fills the cell edge to edge and the table rules close it on all
     four sides, so each key is a box in the grid, joined to the drawing by
     colour. Keys without a tone (ink parts, stage numbers) keep the box and
     stay ink. */

  .post-body :global(table.fig-key th:first-child),
  .post-body :global(table.fig-key td.key) {
    width: 3.4em;
    padding: 0.55em 0.4em;
    border-right: 1px solid var(--line);
    text-align: center;
    vertical-align: middle;
  }

  .post-body :global(td.key .co) {
    font-family: var(--font-code);
    font-size: 0.85rem;
    font-weight: 600;
  }

  .post-body :global(td.key[data-tone]) {
    background: var(--key-tone);
    color: var(--sheet);
  }

  .post-body :global(td.key[data-tone="gpu"]) {
    --key-tone: var(--tone-gpu);
  }

  .post-body :global(td.key[data-tone="ram"]) {
    --key-tone: var(--tone-ram);
  }

  .post-body :global(td.key[data-tone="cache"]) {
    --key-tone: var(--tone-cache);
  }

  .post-body :global(td.key[data-tone="disk"]) {
    --key-tone: var(--tone-disk);
  }

  .post-body :global(td.key[data-tone="hot"]) {
    --key-tone: var(--tone-hot);
  }

  @media (max-width: 760px) {
    .journal {
      display: block;
    }

    .spine {
      position: sticky;
      z-index: 4;
      top: 0;
      max-height: none;
      margin: 0 -1.35em 1.5rem;
      padding: 0.5em 1.35em;
      overflow-x: auto;
      border-bottom: 1px solid var(--stroke);
      background: var(--sheet);
    }

    .sec-label {
      display: none;
    }

    .toc > ol {
      display: flex;
      gap: 1em;
    }

    .toc ol ol {
      display: none;
    }

    .toc a {
      padding: 0.5em 0;
      border-left: 0;
      border-bottom: 2px solid transparent;
      white-space: nowrap;
    }

    .toc a.active {
      border-bottom-color: var(--accent-ink);
    }
  }

  @media (max-width: 600px) {
    .post-page {
      padding-right: 1em;
      padding-left: 1em;
    }
  }
</style>
