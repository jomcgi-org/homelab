<script>
  import { landingResults } from "$lib/public/posts/landing-results.js";
  import { figures } from "$lib/public/posts/ix/index.js";
  import { splitFigures } from "$lib/public/posts/ix/split.js";
  import { replaceState } from "$app/navigation";
  import { page } from "$app/state";
  import { Seo } from "$lib/public/components";
  import SchemeToggle from "$lib/public/components/SchemeToggle.svelte";
  import { formatDate } from "../blog.js";
  import Trail from "../Trail.svelte";

  let { data } = $props();
  let activeId = $state("");
  let hasLanding = $derived(data.slug === "125b-on-a-4090");
  // The landing's two recorded demos: reading a long report (prefill-heavy) and
  // writing a change to a codebase (decode with draft tokens).
  const demos = [
    { kind: "research", label: "Long-context research" },
    { kind: "coding", label: "Agentic coding" },
  ];
  let demo = $state("research");
  const replays = {
    "ember-conformance": {
      load: () => import("$lib/public/posts/ConformanceReplay.svelte"),
    },
  };
  let replay = $derived(replays[data.slug]);
  let single = $derived(!data.toc.length && !hasLanding);

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
    if (target) {
      target.scrollIntoView({ behavior, block: "start" });
      if (id === "post-body") target.focus({ preventScroll: true });
    } else window.scrollTo({ top: 0, behavior });
  }

  $effect(() => {
    const headings = document.querySelectorAll(
      ".post-page h2[id], .post-page h3[id]",
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

<main class="td sheet post-page" class:with-landing={hasLanding}>
  <div class="frame">
    {#if hasLanding}
      <section class="demo-landing" aria-labelledby="landing-title">
        <header class="landing-header">
          <div class="landing-topline">
            <nav aria-label="Blog navigation">
              <a href="/">jomcgi.dev</a><span aria-hidden="true">/</span><a
                href="/blog">blog</a
              >
            </nav>
            <SchemeToggle />
          </div>
          <h1 id="landing-title">{data.title}</h1>
          <p class="landing-hardware">
            No further weight quantisation: the NVFP4 release on one 24 GB card
          </p>
        </header>
        <section class="landing-demo" aria-labelledby="inference-demo">
          <h2 class="sr-only" id="inference-demo">Inference Demo</h2>
          <div class="demo-switch" role="group" aria-label="Demo">
            {#each demos as d}
              <button
                type="button"
                aria-pressed={demo === d.kind}
                onclick={() => (demo = d.kind)}>{d.label}</button
              >
            {/each}
          </div>
          {#await Promise.all( [import("$lib/public/posts/QwenReplay.svelte"), import("$lib/public/posts/agent-replay.json")] )}
            <table class="landing-results">
              <caption>Recorded on the RTX 4090</caption>
              <thead>
                <tr>
                  <th scope="col">Request</th>
                  <th scope="col">Input tokens</th>
                  <th scope="col">First token</th>
                  <th scope="col">Decode</th>
                </tr>
              </thead>
              <tbody>
                {#each landingResults as row}
                  <tr>
                    <th scope="row">{row.label}</th>
                    <td>{row.inputTokens.toLocaleString("en-US")}</td>
                    <td>{row.firstTokenSeconds.toFixed(2)} s</td>
                    <td>{row.decodeRate.toFixed(1)} tok/s</td>
                  </tr>
                {/each}
              </tbody>
            </table>
          {:then [replay, coding]}
            {#key demo}
              {#if demo === "coding"}
                <replay.default
                  landing
                  kind="coding"
                  recording={coding.default}
                />
              {:else}
                <replay.default landing />
              {/if}
            {/key}
          {:catch}
            <p>
              The inference demo could not load. Refresh the page to try again.
            </p>
          {/await}
        </section>
        <a class="read-post" href="#post-body" onclick={onIndexClick}
          >Read the post ↓</a
        >
      </section>
    {/if}
    <div
      class="journal"
      class:single
      id={hasLanding ? "post-body" : undefined}
      tabindex="-1"
    >
      {#if single}
        <div class="trail-inline"><Trail post={data.title} /></div>
      {:else}
        <aside class="spine">
          <div class="trail-dock">
            <Trail post={data.title} />
          </div>
          {#if data.toc.length}
            <nav class="toc index" aria-label="Sections">
              <p class="sec-label">/ Index</p>
              <ol class="index-list">
                {#each data.toc as section}
                  <li>
                    <a
                      href={`#${section.id}`}
                      onclick={onIndexClick}
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
                              class="sub"
                              href={`#${sub.id}`}
                              onclick={onIndexClick}
                              class:active={activeId === sub.id}
                              aria-current={activeId === sub.id
                                ? "location"
                                : undefined}
                              ><span class="num">{split(sub.text)[0]}</span
                              ><span>{split(sub.text)[1]}</span></a
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
        </aside>{/if}

      <div class="post-frame">
        <article class="edition">
          {#if !hasLanding}<p class="ed-head">
              <time datetime={data.date}>{formatDate(data.date)}</time>
            </p>

            <header class="ed-lead">
              <h1>{data.title}</h1>
              {#if !single}<p>{data.summary}</p>{/if}
            </header>{:else}<p class="ed-head">
              <time datetime={data.date}>{formatDate(data.date)}</time>
            </p>
            <header class="ed-lead">
              <h2>{data.title}</h2>
              <p>{data.summary}</p>
            </header>{/if}

          {#if data.preamble}
            <!-- Server-rendered, constrained first-party markdown. -->
            <div class="post-body prose">{@html data.preamble}</div>
          {/if}
          {#if replay && !replay.within}
            <div class="post-body prose">
              {#await replay.load()}
                <p>Loading the recording...</p>
              {:then module}
                <module.default />
              {:catch}
                <p>
                  The recording could not load. Refresh the page to try again.
                </p>
              {/await}
            </div>
          {/if}
        </article>

        <!-- One panel per numbered section: a page flip between them. -->
        {#each data.sections as section}
          {@const replayBoundary = replay?.within
            ? section.indexOf(replay.before)
            : -1}
          {@const showReplay =
            replay?.within &&
            section.includes(replay.within) &&
            replayBoundary >= 0}
          {#if !hasLanding || !section.startsWith('<h2 id="inference-demo"')}
            <section class="edition post-body prose">
              {#each splitFigures(showReplay ? section.slice(0, replayBoundary) : section) as part}
                {@const Figure = part.ix && figures[part.ix]}
                {#if Figure}
                  <Figure />
                {:else if part.ix}
                  {@html part.fallback}
                {:else}
                  {@html part.html}
                {/if}
              {/each}
              {#if showReplay}
                {#await replay.load()}
                  <p>Loading the recording...</p>
                {:then module}
                  <module.default />
                {:catch}
                  <p>
                    The recording could not load. Refresh the page to try again.
                  </p>
                {/await}
                {@html section.slice(replayBoundary)}
              {/if}
            </section>{/if}
        {/each}
      </div>
    </div>
  </div>
</main>

<style>
  /* The post retains its panel-per-section rhythm, first-viewport landing
     and nested numbered TOC. The shared strip has different mobile spacing. */
  .post-page :global(:focus-visible) {
    /* Retain the public reset's focus shape with its existing radius token. */
    border-radius: calc(var(--radius) * 0.75);
  }

  /* HTML prose and the demo selector previously inherited the public reset's
     blue ring. TOC, Trail and embedded controls keep their own ring colours. */
  :global(.post-page .post-body a:focus-visible),
  :global(.post-page .replay a:focus-visible),
  .post-page .demo-switch button:focus-visible {
    outline-color: var(--blue);
  }

  .post-page :global(.replay input[type="range"]) {
    /* The range retains its native control colour rather than sheet ink. */
    color: revert;
  }

  /* The conformance replay already owns two smaller control shapes. Its
     topology identifies that embedded figure without changing other replays. */
  .post-page :global(.replay:has(.topo) button:focus-visible) {
    border-radius: calc(var(--radius) / 4);
  }
  .post-page :global(.replay:has(.topo) button.cell:focus-visible) {
    border-radius: 0;
  }

  .post-page .frame {
    position: static;
  }

  .post-page .demo-landing {
    min-height: calc(100svh - 1.5rem);
    box-sizing: border-box;
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
    padding-bottom: 0.75rem;
  }
  .post-page .landing-topline {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 0.8rem;
  }
  .post-page .landing-topline nav {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    color: var(--ink-2);
    font: 0.7rem var(--font-code);
  }
  .post-page .landing-topline a {
    display: inline-flex;
    align-items: center;
    min-height: 2.25rem;
    color: inherit;
    text-decoration: none;
  }
  .post-page .landing-topline a:hover {
    color: var(--accent-ink);
  }
  .post-page .landing-header h1 {
    margin: 0;
    font-size: clamp(1.4rem, 2.8vw, 2rem);
    line-height: 1.15;
    letter-spacing: -0.025em;
  }
  .post-page .landing-hardware {
    margin: 0.5rem 0 0.25rem;
    color: var(--ink-2);
    font: 0.75rem / 1.5 var(--font-code);
  }
  .post-page .landing-demo {
    min-width: 0;
  }
  .post-page .demo-switch {
    display: flex;
    gap: 0;
    margin-bottom: 0.75rem;
    border: 1px solid var(--ink);
    width: fit-content;
  }
  .post-page .demo-switch button {
    padding: 0.4rem 0.8rem;
    min-height: 2.75rem;
    border: 0;
    background: var(--sheet);
    color: var(--ink);
    font: 0.75rem var(--font-code);
    cursor: pointer;
  }
  .post-page .demo-switch button + button {
    border-left: 1px solid var(--ink);
  }
  .post-page .demo-switch button[aria-pressed="true"] {
    background: var(--ink);
    color: var(--sheet);
  }
  .post-page .landing-results {
    width: 100%;
    border-collapse: collapse;
    font: 0.8rem var(--font-code);
  }
  .post-page .landing-results caption {
    padding-bottom: 0.5rem;
    text-align: left;
    color: var(--ink-2);
  }
  .post-page .landing-results th,
  .post-page .landing-results td {
    padding: 0.4rem 0.5rem 0.4rem 0;
    text-align: left;
    font-weight: normal;
    border-bottom: 1px solid currentColor;
  }
  .post-page .landing-demo h2 {
    /* Reloaded demo fragments keep the opening header in view. */
    scroll-margin-top: 12rem;
  }
  .post-page .read-post {
    align-self: flex-start;
    margin-top: auto;
    color: var(--accent-ink);
    font: 0.75rem var(--font-code);
    text-underline-offset: 0.2em;
    display: inline-flex;
    align-items: center;
    min-height: 2.75rem;
  }
  .with-landing .journal {
    padding-top: 2rem;
    scroll-margin-top: 1.5rem;
    border-top: 1px solid var(--stroke);
  }
  .post-page a:focus-visible {
    outline: 2px solid var(--accent-ink);
    outline-offset: 3px;
  }
  .post-page .trail-inline {
    grid-column: 1;
  }

  .post-page .toc ol {
    margin: 0;
    padding: 0;
    list-style: none;
    border-bottom: 0;
  }

  .post-page .toc a {
    /* The post keeps its tighter rows and UI-font number columns. The
       reusable index supplies links, active rules and sub-entry sizing. */
    gap: 0;
    align-items: normal;
    padding: 0.34em 0 0.34em 0.6rem;
  }

  /* The number is a fixed column so every title starts on the same line
     and a wrapped title hangs under itself, not under its number. */
  .post-page .toc .num {
    font-family: inherit;
    font-size: inherit;
    font-variant-numeric: tabular-nums;
  }

  .post-page .toc a.sub {
    padding-left: 1.4rem;
  }

  .post-page .post-frame {
    max-width: 54em;
    min-width: 0;
  }

  .post-page .edition + .edition {
    margin-top: 1.5rem;
  }

  .post-page .ed-head {
    justify-content: space-between;
    flex-wrap: nowrap;
    gap: normal;
    align-items: normal;
    color: var(--ink);
  }

  .post-page .ed-head time {
    color: var(--ink-2);
  }

  .post-page .ed-lead {
    padding: 1rem;
  }

  .post-page .post-body {
    padding: 0 1rem 1rem;
  }

  .post-page .ed-lead + .post-body {
    border-top: 1px solid var(--stroke);
  }

  /* The panel body counts figures across the whole post, not per panel. */
  .post-page .post-frame {
    counter-reset: fig;
  }

  .post-page .post-body {
    counter-reset: none;
  }

  /* A keyed figure is drawn for a 48em measure. Below that it scrolls
     sideways inside its panel instead of shrinking its 11px labels
     past legibility. */
  .post-page .post-body :global(.fig-art svg) {
    display: block;
    width: 100%;
    min-width: 36em;
    height: auto;
  }

  @media (min-width: 761px) {
    /* Keep the scheme switch reachable while reading the full article. */
    .post-page .landing-topline :global(.scheme) {
      position: fixed;
      top: 1.5rem;
      right: 1.2rem;
      z-index: 1000;
    }
  }

  @media (max-width: 760px) {
    /* The chrome row sits at 1em; the page follows it from here down. */
    .post-page {
      padding-top: 1rem;
      padding-right: 1em;
      padding-left: 1em;
      padding-bottom: 6em;
    }

    /* One quiet header leaves the recording in the first phone viewport.
       Expanded details keep their natural height rather than clipping. */
    .post-page.with-landing {
      padding-top: 0.5rem;
    }
    .post-page .demo-landing {
      min-height: calc(100svh - 0.5rem);
    }
    .post-page .landing-topline {
      margin-bottom: 0.5rem;
    }

    .post-page .spine {
      position: sticky;
      z-index: 4;
      top: 0;
      margin: 0 -1em 1.5rem;
      padding: 0.5em 1em;
      overflow-x: auto;
      border-bottom: 1px solid var(--stroke);
      background: var(--sheet);
    }

    .post-page .trail-dock,
    .post-page .trail-inline,
    .post-page .sec-label {
      display: none;
    }

    .post-page .toc > ol {
      display: flex;
      gap: 1em;
    }

    .post-page .toc ol ol {
      display: none;
    }

    .post-page .toc a {
      padding: 0.5em 0;
      border-left: 0;
      border-bottom: 2px solid transparent;
      white-space: nowrap;
    }

    .post-page .toc .num {
      width: auto;
      margin-right: 0.35em;
    }

    .post-page .toc a.active {
      border-bottom-color: var(--accent-ink);
    }
  }
</style>
