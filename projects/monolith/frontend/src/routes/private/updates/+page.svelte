<script>
  import {
    facetHref,
    formatDate,
    groupUpdatesByMonth,
    label,
  } from "./updates.js";

  let { data } = $props();
  let dark = $state(false);
  $effect(() => {
    const scheme = window.matchMedia("(prefers-color-scheme: dark)");
    const apply = () => (dark = scheme.matches);
    apply();
    scheme.addEventListener("change", apply);
    return () => scheme.removeEventListener("change", apply);
  });
  let activeDate = $state("");
  let activeMonth = $derived(activeDate.slice(0, 7));
  let monthGroups = $derived(groupUpdatesByMonth(data.updates));
  let filtering = $derived(
    Boolean(data.selectedProject || data.selectedTechnology),
  );

  $effect(() => {
    const dates = data.updates.map((update) => update.published_on);
    if (!dates.includes(activeDate)) activeDate = dates[0] ?? "";
  });

  $effect(() => {
    const updates = data.updates;
    if (!updates.length) return;
    const entries = document.querySelectorAll("[data-update-date]");
    const observer = new IntersectionObserver(
      (observed) => {
        const visible = observed
          .filter((item) => item.isIntersecting)
          .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
        if (visible[0]) activeDate = visible[0].target.dataset.updateDate;
      },
      { rootMargin: "-15% 0px -65% 0px" },
    );
    entries.forEach((entry) => observer.observe(entry));
    return () => observer.disconnect();
  });
</script>

<svelte:head>
  <title>Product updates | jomcgi</title>
  <meta
    name="description"
    content="A private daily journal of features and improvements across the homelab."
  />
</svelte:head>

<main class="updates-page shell {dark ? 'night' : 'day'}">
  <div class="frame">
    <header class="masthead">
      <a class="back-link" href="/">&larr; Dashboard</a>
      <span class="masthead-id">
        <h1>Product updates</h1>
        <span class="masthead-meta"
          >/ Private release journal / Daily editions</span
        >
      </span>
    </header>

    <div class="journal">
      <aside class="spine">
        <nav class="spine-filters" aria-label="Filter product updates">
          <p class="sec-label">/ Filter</p>
          {#if data.projects.length}
            <details class="spine-group" open={Boolean(data.selectedProject)}>
              <summary>
                <span class="group-name">Projects</span>
                <span class="group-count">{data.projects.length}</span>
              </summary>
              <div class="spine-list">
                {#each data.projects as facet}
                  <a
                    class:active={data.selectedProject === facet.value}
                    aria-current={data.selectedProject === facet.value
                      ? "true"
                      : undefined}
                    href={facetHref(
                      "project",
                      facet.value,
                      data.selectedProject,
                      data.selectedTechnology,
                    )}
                  >
                    <span class="name">{label(facet.value)}</span>
                    <span class="count"
                      >{facet.count}{#if data.selectedProject === facet.value}
                        <span class="remove" aria-hidden="true">×</span
                        >{/if}</span
                    >
                  </a>
                {/each}
              </div>
            </details>
          {/if}
          {#if data.technologies.length}
            <details
              class="spine-group"
              open={Boolean(data.selectedTechnology)}
            >
              <summary>
                <span class="group-name">Technologies</span>
                <span class="group-count">{data.technologies.length}</span>
              </summary>
              <div class="spine-list">
                {#each data.technologies as facet}
                  <a
                    class:active={data.selectedTechnology === facet.value}
                    aria-current={data.selectedTechnology === facet.value
                      ? "true"
                      : undefined}
                    href={facetHref(
                      "technology",
                      facet.value,
                      data.selectedProject,
                      data.selectedTechnology,
                    )}
                  >
                    <span class="name">{label(facet.value)}</span>
                    <span class="count"
                      >{facet.count}{#if data.selectedTechnology === facet.value}
                        <span class="remove" aria-hidden="true">×</span
                        >{/if}</span
                    >
                  </a>
                {/each}
              </div>
            </details>
          {/if}
          {#if filtering && !data.error}
            <p class="filter-results">
              <span>
                {data.updates.length}
                {data.updates.length === 1
                  ? "update"
                  : "updates"}{#if data.selectedProject}
                  · {label(
                    data.selectedProject,
                  )}{/if}{#if data.selectedTechnology}
                  · {label(data.selectedTechnology)}{/if}
              </span>
              <a class="clear" href="/updates">Clear</a>
            </p>
          {/if}
        </nav>

        {#if data.updates.length}
          <nav class="date-rail" aria-label="Updates by date">
            <p class="sec-label">/ Index</p>
            {#each monthGroups as group}
              <details class="rail-month" open={group.key === activeMonth}>
                <summary>
                  <span class="month-name">{group.label}</span>
                  <span class="group-count">{group.updates.length}</span>
                </summary>
                <div class="rail-days">
                  {#each group.updates as update}
                    <a
                      class:active={activeDate === update.published_on}
                      href={`#update-${update.published_on}`}
                      aria-current={activeDate === update.published_on
                        ? "location"
                        : undefined}
                    >
                      <span>{update.published_on.slice(8)}</span>
                      <small>{update.headline}</small>
                    </a>
                  {/each}
                </div>
              </details>
            {/each}
          </nav>
        {/if}
      </aside>

      <div class="content">
        {#if data.error}
          <div class="state" role="alert">
            <strong>The journal is unavailable.</strong>
            <span>The archive service did not respond. Try again shortly.</span>
          </div>
        {:else if !data.updates.length}
          {#if filtering}
            <div class="state">
              <strong>No updates match these filters.</strong>
              <span>Clear a filter to see the complete journal.</span>
            </div>
          {:else}
            <section class="empty-entry">
              <p class="sec-label">/ Status</p>
              <div class="empty-meta">
                <span class="ready-dot"></span>
                <span>Archive ready</span>
                <span>Edition 00</span>
              </div>
              <h2>Waiting for the first daily edition.</h2>
              <p>
                New features and meaningful improvements will land here after
                the daily rollup submits a validated entry.
              </p>

              <div class="empty-flow" aria-label="Daily update workflow">
                <div>
                  <span>01</span>
                  <strong>Collect changes</strong>
                  <small>Public commit range</small>
                </div>
                <div>
                  <span>02</span>
                  <strong>Write the story</strong>
                  <small>Features, not commit logs</small>
                </div>
                <div>
                  <span>03</span>
                  <strong>Publish here</strong>
                  <small>Structured and browsable</small>
                </div>
              </div>
            </section>
          {/if}
        {:else}
          <section class="editions" aria-label="Product update entries">
            {#each data.updates as update}
              <article
                class="edition"
                id={`update-${update.published_on}`}
                data-update-date={update.published_on}
              >
                <p class="ed-head">
                  <time datetime={update.published_on}
                    >{formatDate(update.published_on)}</time
                  >
                  <span class="ed-category">{label(update.category)}</span>
                </p>

                <div class="ed-article">
                  <h2>{update.headline}</h2>
                  <p class="summary">{update.summary}</p>

                  <div class="meta-rows">
                    <p class="meta-kv">
                      <span class="k">Filed</span>
                      <span class="v filed-tags">
                        {#each update.projects as project}
                          <a
                            href={facetHref(
                              "project",
                              project,
                              data.selectedProject,
                              data.selectedTechnology,
                            )}>{label(project)}</a
                          >
                        {/each}
                        {#each update.technologies as technology}
                          <a
                            href={facetHref(
                              "technology",
                              technology,
                              data.selectedProject,
                              data.selectedTechnology,
                            )}>{label(technology)}</a
                          >
                        {/each}
                      </span>
                    </p>
                    <p class="meta-kv">
                      <span class="k">Source</span>
                      <a
                        class="v source-link"
                        href={update.source_compare_url}
                        target="_blank"
                        rel="noreferrer"
                        >{update.source_commit_count}
                        {update.source_commit_count === 1
                          ? "commit"
                          : "commits"} on GitHub
                        <span aria-hidden="true">&nearr;</span></a
                      >
                    </p>
                  </div>

                  <p class="sec-sublabel">What is new</p>
                  <div class="items">
                    {#each update.highlights as item}
                      <div class="item">
                        <h3>{item.title}</h3>
                        <p>{item.description}</p>
                      </div>
                    {/each}
                  </div>

                  {#if update.improvements.length}
                    <p class="sec-sublabel">Also improved</p>
                    <div class="items supporting">
                      {#each update.improvements as item}
                        <div class="item">
                          <h3>{item.title}</h3>
                          <p>{item.description}</p>
                        </div>
                      {/each}
                    </div>
                  {/if}
                </div>
              </article>
            {/each}
          </section>
        {/if}
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

  :global(body) {
    margin: 0;
  }

  .updates-page {
    /* Accent mixed toward ink so small accent text and accent fills clear
       WCAG AA in every period palette; bare var(--accent) measures 3.85:1
       on dawn paper. */
    --accent-ink: color-mix(in srgb, var(--accent) 85%, var(--ink));
    /* Rule and box borders: --line is too faint to carry the drawing on
       the night palette, so structural strokes get a stronger mix. */
    --stroke: color-mix(in srgb, var(--ink) 28%, transparent);
    min-height: 100vh;
    box-sizing: border-box;
    padding: clamp(2rem, 4vh, 3rem) clamp(1.5em, 5vw, 4.5em) 6em;
    color: var(--ink);
    background: var(--paper);
    font-family: var(--font-ui);
    font-size: 16px;
  }

  .updates-page a:focus-visible,
  .updates-page summary:focus-visible {
    outline: 2px solid var(--accent-ink);
    outline-offset: 3px;
  }

  .frame {
    position: relative;
    /* Sheet width = spine + gap + a 48em text measure: the ruled strokes
       end where the text ends, so no rule implies content that is not
       there. */
    max-width: 69em;
    margin: 0 auto;
  }

  /* ── Section labels: the one heading style for all chrome ── */

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

  /* ── Masthead ── */

  /* One functional line: navigation left, identity right. The former big
     h1 tier and standfirst competed with the edition headline for
     attention and carried no function. */
  .masthead {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    gap: 2em;
    align-items: baseline;
    margin-bottom: 2.2rem;
    padding-bottom: 0.9rem;
    /* Hard partition: the drawing's frame line, not a hairline. */
    border-bottom: 2px solid var(--ink);
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.72rem;
    white-space: nowrap;
  }

  .back-link {
    color: var(--ink-2);
    text-decoration: none;
  }

  .back-link:hover {
    color: var(--accent-ink);
  }

  .masthead-id {
    display: inline-flex;
    gap: 0.5em;
    align-items: baseline;
    justify-self: end;
    min-width: 0;
    overflow: hidden;
  }

  h1 {
    display: inline;
    margin: 0;
    color: var(--ink);
    font-family: var(--font-code);
    font-size: 1em;
    font-weight: 700;
    letter-spacing: inherit;
  }

  .masthead-meta {
    margin: 0;
  }

  /* ── Layout ── */

  .journal {
    display: grid;
    grid-template-columns: minmax(12em, 15em) minmax(0, 48em);
    gap: clamp(2em, 4vw, 4em);
  }

  .spine {
    position: sticky;
    top: 1.5rem;
    align-self: start;
    max-height: calc(100vh - 3rem);
    overflow-y: auto;
    scrollbar-width: none;
    /* Left padding keeps the offset focus ring from being clipped by this
       element's own scroll container. */
    padding-left: 0.4rem;
  }

  /* ── Spine: filters ── */

  .spine-group summary {
    display: flex;
    gap: 0.6em;
    align-items: baseline;
    justify-content: space-between;
    padding: 0.5em 0;
    border-bottom: 1px solid var(--line);
    cursor: pointer;
    list-style: none;
  }

  .spine-group summary::-webkit-details-marker {
    display: none;
  }

  .group-name {
    font-size: 0.8em;
    font-weight: 600;
  }

  .spine-group summary:hover .group-name {
    color: var(--accent-ink);
  }

  .group-count {
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.7em;
  }

  .group-count::after {
    padding-left: 0.5em;
    content: "+";
  }

  .spine-group[open] .group-count::after,
  .rail-month[open] .group-count::after {
    content: "\2212";
  }

  .spine-list {
    padding: 0.35em 0;
    border-bottom: 1px solid var(--line);
  }

  .spine-list a {
    display: flex;
    gap: 0.6em;
    align-items: baseline;
    justify-content: space-between;
    padding: 0.34em 0 0.34em 0.6rem;
    border-left: 2px solid transparent;
    color: var(--ink-2);
    font-size: 0.8em;
    text-decoration: none;
    transition: color 120ms ease;
  }

  .spine-list a:hover,
  .spine-list a.active {
    color: var(--accent-ink);
  }

  .spine-list a.active {
    border-left-color: var(--accent-ink);
    font-weight: 650;
  }

  .spine-list .count {
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.85em;
  }

  .spine-list a.active .count {
    color: inherit;
  }

  .filter-results {
    display: flex;
    flex-wrap: wrap;
    gap: 0.3rem 0.6rem;
    align-items: baseline;
    margin: 0.8em 0 0;
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.72rem;
  }

  .clear {
    color: var(--ink-2);
    border-bottom: 1px solid currentColor;
    text-decoration: none;
  }

  .clear:hover {
    color: var(--accent-ink);
  }

  /* ── Spine: index ── */

  .date-rail {
    margin-top: 1.8rem;
  }

  .rail-month summary {
    display: flex;
    gap: 0.6em;
    align-items: baseline;
    justify-content: space-between;
    padding: 0.5em 0;
    border-bottom: 1px solid var(--line);
    cursor: pointer;
    list-style: none;
  }

  .rail-month summary::-webkit-details-marker {
    display: none;
  }

  .month-name {
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.68em;
    font-weight: 500;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .rail-month summary:hover .month-name {
    color: var(--accent-ink);
  }

  .rail-days {
    padding: 0.35em 0;
    border-bottom: 1px solid var(--line);
  }

  .rail-month a {
    display: grid;
    grid-template-columns: 2em minmax(0, 1fr);
    gap: 0.65em;
    align-items: start;
    padding: 0.42em 0 0.42em 0.6rem;
    border-left: 2px solid transparent;
    color: var(--ink-2);
    text-decoration: none;
    transition: color 120ms ease;
  }

  .rail-month a > span {
    font-family: var(--font-code);
    font-size: 0.75em;
  }

  .rail-month small {
    display: -webkit-box;
    overflow: hidden;
    font-size: 0.7em;
    line-height: 1.3;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
  }

  .rail-month a:hover,
  .rail-month a.active {
    color: var(--accent-ink);
  }

  .rail-month a.active {
    border-left-color: var(--accent-ink);
  }

  .rail-month a.active > span {
    font-weight: 700;
  }

  /* ── Editions ── */

  .edition {
    scroll-margin-top: 1.5rem;
  }

  .edition + .edition {
    margin-top: 2.5rem;
    padding-top: 2rem;
    /* Hard partition between editions, same weight as the frame line. */
    border-top: 2px solid var(--ink);
  }

  /* ── Edition head and metadata rows ──
     The content column follows the spine's discipline: label left, value
     right, one hairline per row. A slash-labeled edition banner, boxed
     chips, and a free-wrapping filed line were all tried and vetoed as
     noise the spine never needed. */

  .ed-head {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 1.5em;
    align-items: baseline;
    margin: 0;
    padding: 0.5em 0;
    border-bottom: 1px solid var(--line);
    font-family: var(--font-code);
    font-size: 0.72rem;
  }

  .ed-head time {
    color: var(--ink-2);
  }

  .ed-category {
    color: var(--accent-ink);
    font-weight: 500;
  }

  .meta-rows {
    margin: 0 0 1.2rem;
  }

  /* The following section label draws the closing rule; a border here too
     left a stray line bracketing empty space. */
  .meta-rows .meta-kv:last-child {
    border-bottom: 0;
  }

  /* Spec-table shape: fixed label column, value flows left beside it.
     The spine's space-between works for short counts; a long value list
     right-aligned against it wraps ragged with a gulf in the middle. */
  .meta-kv {
    display: grid;
    grid-template-columns: 5.5em minmax(0, 1fr);
    gap: 1.4em;
    align-items: baseline;
    margin: 0;
    padding: 0.45em 0;
    border-bottom: 1px solid var(--line);
  }

  .meta-kv .k {
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.72rem;
  }

  .meta-kv .v {
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.72rem;
  }

  .filed-tags {
    display: inline-flex;
    flex-wrap: wrap;
    gap: 0.4em;
    align-items: baseline;
  }

  .filed-tags a {
    padding: 0.1em 0.45em;
    border: 1px solid var(--stroke);
    color: var(--ink-2);
    text-decoration: none;
  }

  .filed-tags a:hover {
    border-color: var(--accent-ink);
  }

  .filed-tags a:hover {
    color: var(--accent-ink);
  }

  .source-link {
    color: var(--ink-2);
    text-decoration: none;
  }

  .source-link:hover {
    color: var(--accent-ink);
  }

  /* ── Edition article ── */

  .ed-article h2 {
    max-width: 34ch;
    margin: 1rem 0 0;
    font-size: clamp(1.25rem, 1.9vw, 1.6rem);
    font-weight: 700;
    letter-spacing: -0.02em;
    line-height: 1.15;
  }

  .summary {
    margin: 0.7rem 0 1.1rem;
    color: var(--ink);
    font-size: 0.88rem;
    line-height: 1.5;
  }

  .sec-sublabel {
    margin: 0 0 0.9em;
    padding: 0.45em 0 0;
    border-top: 1px solid var(--stroke);
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.72rem;
    font-weight: 500;
  }

  .items {
    margin-bottom: 1.4rem;
  }

  .item + .item {
    margin-top: 0.9em;
  }

  .item h3 {
    margin: 0 0 0.2em;
    font-size: 0.84rem;
    font-weight: 650;
  }

  .item p {
    margin: 0;
    color: var(--ink-2);
    font-size: 0.8rem;
    line-height: 1.5;
  }

  .supporting .item h3 {
    font-size: 0.85rem;
  }

  .supporting .item p {
    font-size: 0.82rem;
  }

  /* ── States ── */

  .state {
    display: grid;
    gap: 0.5em;
    padding: 1.5em;
    border: 1px solid var(--stroke);
  }

  .state span {
    color: var(--ink-2);
  }

  .empty-meta {
    display: flex;
    flex-wrap: wrap;
    gap: 0.65em 1.1em;
    align-items: center;
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.68em;
    text-transform: uppercase;
  }

  .empty-meta span:last-child {
    margin-left: auto;
  }

  .ready-dot {
    width: 0.45em;
    height: 0.45em;
    border-radius: 50%;
    background: var(--ok);
    box-shadow: 0 0 0 0.2em color-mix(in srgb, var(--ok) 13%, transparent);
  }

  .empty-entry h2 {
    max-width: 22ch;
    margin: 1.2rem 0 0;
    font-size: clamp(1.5rem, 2.6vw, 2.2rem);
    font-weight: 700;
    letter-spacing: -0.025em;
    line-height: 1.12;
  }

  .empty-entry > p {
    margin: 1rem 0 0;
    color: var(--ink-2);
    font-size: 0.95em;
    line-height: 1.65;
  }

  .empty-flow {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    margin-top: 2.2em;
    border-top: 1px solid var(--stroke);
    border-bottom: 1px solid var(--stroke);
  }

  .empty-flow > div {
    display: grid;
    align-content: start;
    min-width: 0;
    padding: 1.1em 1em 1.2em 0;
  }

  .empty-flow > div + div {
    padding-left: 1em;
    border-left: 1px solid var(--line);
  }

  .empty-flow span {
    color: var(--accent-ink);
    font-family: var(--font-code);
    font-size: 0.62em;
  }

  .empty-flow strong {
    margin-top: 1.2em;
    font-size: 0.92em;
    font-weight: 650;
  }

  .empty-flow small {
    margin-top: 0.35em;
    color: var(--ink-2);
    font-size: 0.7em;
    line-height: 1.45;
  }

  /* The folio is the first thing to go when the masthead track tightens;
     a wrapped or clipped identity string is worse than a shorter one. */
  @media (max-width: 980px) {
    .masthead-meta {
      display: none;
    }
  }

  /* ── Mobile ── */

  @media (max-width: 760px) {
    .updates-page {
      padding: 1.8rem 1.35em 4em;
    }

    .journal {
      display: block;
    }

    .spine {
      position: static;
      max-height: none;
      padding-left: 0;
      overflow: visible;
    }

    .spine-group summary {
      padding: 0.9em 0;
    }

    .spine-list {
      display: flex;
      flex-wrap: wrap;
      gap: 0 1.1em;
    }

    .spine-list a {
      padding: 0.62em 0;
      border-left: 0;
      border-bottom: 2px solid transparent;
    }

    .spine-list a.active {
      border-bottom-color: var(--accent-ink);
    }

    .date-rail {
      position: sticky;
      z-index: 4;
      top: 0;
      display: flex;
      gap: 1.5em;
      align-items: baseline;
      margin: 1.2rem -1.35em 2rem;
      padding: 0.5em 1.35em;
      overflow-x: auto;
      border-top: 1px solid var(--line);
      border-bottom: 1px solid var(--stroke);
      background: color-mix(in srgb, var(--paper) 92%, transparent);
      backdrop-filter: blur(14px);
    }

    .date-rail > .sec-label {
      display: none;
    }

    .rail-month {
      display: flex;
      gap: 0.65em;
      align-items: center;
      margin: 0;
    }

    .rail-month summary {
      min-width: max-content;
      padding: 0.65em 0;
      border-bottom: 2px solid transparent;
    }

    .rail-days {
      display: flex;
      gap: 0.35em;
      align-items: center;
      padding: 0;
      border-bottom: 0;
    }

    .rail-month a {
      display: block;
      min-width: 2.2em;
      padding: 0.65em 0.4em;
      text-align: center;
      /* In the horizontal strip the desktop left bar would read as a
         divider between pills, so the marker moves to an underline. */
      border-left: 0;
      border-bottom: 2px solid transparent;
    }

    .rail-month a.active {
      border-bottom-color: var(--accent-ink);
    }

    .rail-month small {
      display: none;
    }

    .empty-flow {
      grid-template-columns: 1fr;
    }

    .empty-flow > div {
      grid-template-columns: 2.5em 1fr;
      padding: 1em 0;
    }

    .empty-flow > div + div {
      padding-left: 0;
      border-top: 1px solid var(--line);
      border-left: 0;
    }

    .empty-flow strong,
    .empty-flow small {
      grid-column: 2;
    }

    .empty-flow strong {
      margin-top: 0;
    }
  }
</style>
