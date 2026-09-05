<script>
  import { replaceState } from "$app/navigation";
  import { page } from "$app/state";
  import { Seo } from "$lib/public/components";
  import { formatDate } from "../blog.js";
  import Trail from "../Trail.svelte";

  let { data } = $props();
  let activeId = $state("");

  // "5.1 Prefill was copying whole layers" -> ["5.1", "Prefill was ..."] so
  // the number sits in its own fixed column and the titles align.
  function split(text) {
    const m = /^(\d+(?:\.\d+)*\.?)\s+(.*)$/.exec(text);
    return m ? [m[1], m[2]] : ["", text];
  }

  // The spine follows the reader: the topmost heading in the upper band of
  // the viewport is the active one, and the URL hash follows it so a copied
  // link lands on the section being read. Two cases the band cannot decide
  // on its own: a clicked entry stays active until the scroll it started
  // has stopped (the first and last sections never reach the band, there
  // is no room above or below to absorb the scroll), and the last heading
  // wins once the page is scrolled to its end. The first entry goes to the
  // top of the page rather than to its heading, so the title comes too.
  let pinned = false;
  let settle = 0;
  let first = "";
  let last = "";

  // The hash follows the reader through SvelteKit's replaceState, never
  // history.replaceState directly: the raw call drops the router's index
  // off the history entry and the Back button stops navigating.
  function setHash(id) {
    replaceState(`#${id}`, page.state);
  }

  function setActive(id) {
    if (id === activeId) return;
    activeId = id;
    setHash(id);
  }

  function pin(id) {
    pinned = true;
    activeId = id;
  }

  function onIndexClick(event) {
    // A modified click (new tab, new window) keeps its native meaning.
    if (
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey
    )
      return;
    const link = event.target.closest("a[href^='#']");
    if (!link) return;
    const id = link.getAttribute("href").slice(1);
    const target = id === first ? null : document.getElementById(id);
    if (id !== first && !target) return;
    event.preventDefault();
    pin(id);
    setHash(id);
    const behavior = window.matchMedia("(prefers-reduced-motion: reduce)")
      .matches
      ? "auto"
      : "smooth";
    if (target) target.scrollIntoView({ behavior, block: "start" });
    else window.scrollTo({ top: 0, behavior });
  }

  $effect(() => {
    const headings = document.querySelectorAll(
      ".post-frame h2[id], .post-frame h3[id]",
    );
    if (!headings.length) return;
    first = headings[0].id;
    last = headings[headings.length - 1].id;
    if (location.hash.length > 1) {
      // A malformed escape in the hash must not take the index down with it.
      try {
        pin(decodeURIComponent(location.hash.slice(1)));
      } catch {
        // Leave the band to decide.
      }
    }

    const atEnd = () =>
      window.innerHeight + window.scrollY >=
      document.documentElement.scrollHeight - 2;

    // The heading being read is the last one whose top has passed the
    // reading line at 30% of the viewport (so a fragment that lands a
    // heading at the very top still counts as that heading). Used by the
    // observer and, once a pinned scroll settles, by a direct re-read.
    const bandPick = () => {
      if (atEnd()) return last;
      if (window.scrollY === 0) return first;
      const line = window.innerHeight * 0.3;
      let best = null;
      for (const h of headings) {
        const top = h.getBoundingClientRect().top;
        if (top <= line + 1) best = h.id;
      }
      return best ?? first;
    };

    const observer = new IntersectionObserver(
      () => {
        if (pinned) return;
        const id = bandPick();
        if (id) setActive(id);
      },
      { rootMargin: "-10% 0px -70% 0px" },
    );
    headings.forEach((h) => observer.observe(h));

    // A scroll that has gone quiet for a beat has stopped: release the
    // pin and read the page as it now stands. scrollend is not universal,
    // so the quiet period is the mechanism and scrollend only shortens it.
    const release = () => {
      pinned = false;
      const id = bandPick();
      if (id) setActive(id);
    };
    const onScroll = () => {
      if (!pinned) {
        if (atEnd()) setActive(last);
        return;
      }
      clearTimeout(settle);
      settle = setTimeout(release, 160);
    };
    const onScrollEnd = () => {
      if (!pinned) return;
      clearTimeout(settle);
      release();
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("scrollend", onScrollEnd);
    return () => {
      observer.disconnect();
      clearTimeout(settle);
      window.removeEventListener("scroll", onScroll);
      window.removeEventListener("scrollend", onScrollEnd);
    };
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
        <div class="trail-dock">
          <Trail post={data.title} />
        </div>
        {#if data.toc.length}
          <nav class="toc" aria-label="Sections" onclick={onIndexClick}>
            <p class="sec-label">/ Index</p>
            <ol>
              {#each data.toc as section}
                <li>
                  <a
                    href={`#${section.id}`}
                    class:active={activeId === section.id}
                    aria-current={activeId === section.id
                      ? "location"
                      : undefined}
                    ><span class="num">{split(section.text)[0]}</span><span
                      >{split(section.text)[1]}</span
                    ></a
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
                              : undefined}
                            ><span class="num">{split(sub.text)[0]}</span><span
                              >{split(sub.text)[1]}</span
                            ></a
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
          {@const replayBoundary = section.indexOf('<h3 id="32-improvements"')}
          {@const showReplay =
            data.slug === "125b-on-a-4090" &&
            section.includes('<h3 id="31-4090-demo"') &&
            replayBoundary >= 0}
          <section class="edition post-body">
            {@html showReplay ? section.slice(0, replayBoundary) : section}
            {#if showReplay}
              {#await import("$lib/public/posts/QwenReplay.svelte")}
                <p>Loading the recorded conversation...</p>
              {:then replay}
                <replay.default />
              {:catch}
                <p>
                  The recorded conversation could not load. Refresh the page to
                  try again.
                </p>
              {/await}
              {@html section.slice(replayBoundary)}
            {/if}
          </section>
        {/each}
      </div>
    </div>
  </div>
</main>

<style>
  .post-page {
    box-sizing: border-box;
    min-height: 100vh;
    padding: 1.5rem clamp(4rem, 5vw, 4.5em) 6em;
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

  .trail-dock {
    margin-bottom: 1.4rem;
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
    display: flex;
    padding: 0.34em 0 0.34em 0.6rem;
    border-left: 2px solid transparent;
    color: var(--ink-2);
    font-size: 0.8em;
    line-height: 1.3;
    text-decoration: none;
  }

  /* The number is a fixed column so every title starts on the same line
     and a wrapped title hangs under itself, not under its number. */
  .toc .num {
    flex: none;
    width: 1.7em;
    font-variant-numeric: tabular-nums;
  }

  .toc ol ol a {
    padding-left: 1.4rem;
    font-size: 0.74em;
  }

  .toc ol ol .num {
    width: 2.3em;
  }

  .toc a:hover,
  .toc a.active {
    color: var(--accent-ink);
  }

  /* Colour and the rule mark the active entry; no weight change, which
     would rewrap the line and jog the whole index on every change. */
  .toc a.active {
    border-left-color: var(--accent-ink);
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

  .ed-head time {
    color: var(--ink-2);
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

  /* Column labels sit on the sheet, framed by stroke-weight rules above,
     below, and between them, while data rows keep hairlines. One band per
     panel, the title's; the header is a drawn grid line, not a second
     band, so it never merges with the title or reads as the first row. */
  .post-body :global(th) {
    padding: 0.5em 1rem;
    border-top: 1px solid var(--stroke);
    border-bottom: 1px solid var(--stroke);
    background: none;
    color: var(--ink);
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

  .post-body :global(td + td) {
    border-left: 1px solid var(--line);
  }

  .post-body :global(th + th) {
    border-left: 1px solid var(--stroke);
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

  /* The drawing is flush with the panel: no padding, so an operating
     sequence's partitions meet the panel walls and the figure's rules,
     and those four lines are its outline. Exploded views carry their own
     margin inside the canvas. */
  .post-body :global(.fig-art) {
    padding: 0;
    overflow-x: auto;
    /* The scroll container's own right border sits on the same pixel as
       the panel wall (one pixel of negative margin), so it is invisible
       when the drawing fits and closes the drawing's last cell when it
       scrolls on a narrow screen. */
    margin-right: -1px;
    border-right: 1px solid var(--ink);
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

  /* The caption leads the drawing, as a stroke-framed title row like a
     table header (no band: the panel's title keeps the only one). The
     figure's own top rule is the row's top; its bottom rule opens the art. */
  .post-body :global(.fig figcaption) {
    padding: 0.45em 1rem;
    border-bottom: 1px solid var(--stroke);
    color: var(--ink);
    font-family: var(--font-code);
    font-size: 0.72rem;
  }

  .post-body :global(.fig figcaption::before) {
    content: "Fig. " counter(fig) "  ";
    font-weight: 600;
    letter-spacing: 0.08em;
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

  .post-body :global(tr:last-child td) {
    border-bottom: 1px solid var(--stroke);
  }

  .post-body :global(figure.fig + table th:first-child),
  .post-body :global(figure.fig + table td:first-child) {
    width: 3.5em;
    font-family: var(--font-code);
    text-align: center;
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

  /* A solid block of a tone reads heavier than the 1.25px outline it
     stands for, so the cell carries a tint of the tone and the letter the
     tone itself: the same hue as the drawing's lines, at line weight. */
  .post-body :global(td.key[data-tone]) {
    background: color-mix(in srgb, var(--key-tone) 18%, var(--sheet));
    color: color-mix(in srgb, var(--key-tone) var(--key-letter), var(--ink));
  }

  .post-body :global(td.key[data-tone="gpu"]) {
    --key-tone: var(--ink);
  }

  .post-body :global(td.key[data-tone="ram"]) {
    --key-tone: var(--ink);
  }

  .post-body :global(td.key[data-tone="cache"]) {
    --key-tone: var(--ink);
  }

  .post-body :global(td.key[data-tone="disk"]) {
    --key-tone: var(--ink);
  }

  .post-body :global(td.key[data-tone="hot"]) {
    --key-tone: var(--replay-hot);
  }
  .post-body :global(td.key[data-tone="warm"]) {
    --key-tone: var(--replay-warm);
  }
  .post-body :global(td.key[data-tone="cold"]) {
    --key-tone: var(--replay-cold);
  }
  .post-body :global(td.key[data-tone="reference"]) {
    --key-tone: var(--accent-ink);
  }

  @media (max-width: 760px) {
    /* The chrome row sits at 1em; the page follows it from here down. */
    .post-page {
      padding-top: 1rem;
      padding-right: 1em;
      padding-left: 1em;
    }

    .journal {
      display: block;
    }

    .spine {
      position: sticky;
      z-index: 4;
      top: 0;
      max-height: none;
      margin: 0 -1em 1.5rem;
      padding: 0.5em 1em;
      overflow-x: auto;
      border-bottom: 1px solid var(--stroke);
      background: var(--sheet);
    }

    .trail-dock,
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

    .toc .num {
      width: auto;
      margin-right: 0.35em;
    }

    .toc a.active {
      border-bottom-color: var(--accent-ink);
    }
  }
</style>
