<script>
  import { periodForHour } from "$lib/private/period.js";
  import {
    facetHref,
    formatDate,
    groupUpdatesByMonth,
    label,
  } from "./updates.js";

  let { data } = $props();
  let hour = $state(new Date().getHours());
  let period = $derived(periodForHour(hour));
  $effect(() => {
    const id = setInterval(() => (hour = new Date().getHours()), 60_000);
    return () => clearInterval(id);
  });
  let activeDate = $state("");
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

<main class="updates-page shell {period}">
  <div class="journal">
    <aside class="spine">
      <div class="spine-brand">
        <p class="kicker">Private release journal</p>
        <span class="spine-sub">Daily editions</span>
      </div>

      <nav class="spine-filters" aria-label="Filter product updates">
        {#if data.projects.length}
          <p class="spine-label">Projects</p>
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
                    <span class="remove" aria-hidden="true">×</span>{/if}</span
                >
              </a>
            {/each}
          </div>
        {/if}
        {#if data.technologies.length}
          <p class="spine-label">Technologies</p>
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
                    <span class="remove" aria-hidden="true">×</span>{/if}</span
                >
              </a>
            {/each}
          </div>
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
          <p class="spine-label">By date</p>
          {#each monthGroups as group}
            <div class="rail-month">
              <p>{group.label}</p>
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
          {/each}
        </nav>
      {/if}
    </aside>

    <div class="content">
      <header class="nameplate">
        <h1>Product updates</h1>
        <p class="standfirst">
          What became possible across the homelab, with the exact source range
          behind every update.
        </p>
      </header>

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
            <div class="empty-meta">
              <span class="ready-dot"></span>
              <span>Archive ready</span>
              <span>Edition 00</span>
            </div>
            <h2>Waiting for the first daily edition.</h2>
            <p>
              New features and meaningful improvements will land here after the
              daily rollup submits a validated entry.
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
              <div class="dateline">
                <span class="ed-label">Daily edition</span>
                <time datetime={update.published_on}
                  >{formatDate(update.published_on)}</time
                >
                <span class="ed-category">{label(update.category)}</span>
              </div>

              <h2>{update.headline}</h2>
              <p class="deck">{update.summary}</p>

              <div
                class="edition-body"
                class:has-briefs={update.improvements.length}
              >
                <div class="stories">
                  {#each update.highlights as item}
                    <div class="story">
                      <h3>{item.title}</h3>
                      <p>{item.description}</p>
                    </div>
                  {/each}
                </div>

                {#if update.improvements.length}
                  <aside class="briefs">
                    <h3 class="briefs-label">Also improved</h3>
                    {#each update.improvements as item}
                      <div class="brief">
                        <h4>{item.title}</h4>
                        <p>{item.description}</p>
                      </div>
                    {/each}
                  </aside>
                {/if}
              </div>

              <footer class="folio-line">
                <span class="filed">Filed under</span>
                <span class="folio-tags">
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
                <a
                  class="source-link"
                  href={update.source_compare_url}
                  target="_blank"
                  rel="noreferrer"
                  >{update.source_commit_count}
                  {update.source_commit_count === 1 ? "commit" : "commits"} on GitHub
                  <span aria-hidden="true">&nearr;</span></a
                >
              </footer>
            </article>
          {/each}
        </section>
      {/if}
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
       on dawn paper. Defined once here, used for every accent-colored
       text element below. */
    --accent-ink: color-mix(in srgb, var(--accent) 85%, var(--ink));
    min-height: 100vh;
    box-sizing: border-box;
    padding: clamp(2.5rem, 5vh, 4rem) clamp(1.5em, 6vw, 5.5em) 6em;
    color: var(--ink);
    background:
      radial-gradient(circle at 85% 4%, var(--glow-a), transparent 27em),
      radial-gradient(circle at 8% 24%, var(--glow-b), transparent 24em),
      var(--paper);
    font-family: var(--font-ui);
    font-size: 16px;
  }

  .updates-page a:focus-visible {
    outline: 2px solid var(--accent-ink);
    outline-offset: 3px;
  }

  .journal {
    display: grid;
    grid-template-columns: minmax(11em, 15em) minmax(0, 52em);
    gap: clamp(2em, 5vw, 5em);
    justify-content: center;
    max-width: 76em;
    margin: 0 auto;
  }

  /* ── Spine ─────────────────────────────────── */

  .spine {
    position: sticky;
    top: 2rem;
    align-self: start;
    max-height: calc(100vh - 4rem);
    overflow-y: auto;
    scrollbar-width: none;
    /* Left padding keeps the 3px-offset focus ring from being clipped by
       this element's own scroll container. */
    padding-left: 0.4rem;
  }

  .kicker {
    margin: 0;
    color: var(--accent-ink);
    font-size: 0.7em;
    font-weight: 700;
    letter-spacing: 0.14em;
    text-transform: uppercase;
  }

  .spine-sub {
    display: block;
    margin-top: 0.5em;
    color: var(--ink-3);
    font-family: var(--font-code);
    font-size: 0.68em;
  }

  .spine-label {
    margin: 0 0 0.7em;
    padding-top: 1.1em;
    border-top: 1px solid var(--line);
    color: var(--ink-3);
    font-size: 0.62em;
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .spine-filters {
    margin-top: 1.6rem;
  }

  .spine-list {
    margin-bottom: 1.4rem;
  }

  .spine-list a {
    display: flex;
    gap: 0.6em;
    align-items: baseline;
    justify-content: space-between;
    padding: 0.38em 0 0.38em 0.6rem;
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
    margin: 0 0 1.4rem;
    padding-left: 0.6rem;
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

  /* ── Date rail ─────────────────────────────── */

  .rail-month + .rail-month {
    margin-top: 1.4em;
  }

  .rail-month > p {
    margin: 0 0 0.55em;
    /* Flush with the day rows, whose content starts after the 2px marker
       border plus 0.6rem padding. */
    padding-left: calc(0.6rem + 2px);
    color: var(--ink-3);
    font-size: 0.65em;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }

  .rail-month a {
    display: grid;
    grid-template-columns: 2em minmax(0, 1fr);
    gap: 0.65em;
    align-items: start;
    padding: 0.45em 0 0.45em 0.6rem;
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

  /* ── Nameplate ─────────────────────────────── */

  .nameplate {
    margin-bottom: 2rem;
    padding-bottom: 1.1rem;
    /* Double rule as masthead furniture; ink-2 rather than the near
       invisible --line so it survives the night palette. */
    border-bottom: 3px double var(--ink-2);
  }

  h1 {
    margin: 0;
    font-family: var(--font-display);
    font-size: clamp(2rem, 3.5vw, 2.8rem);
    font-weight: 480;
    letter-spacing: -0.035em;
    line-height: 1;
  }

  .standfirst {
    max-width: 40em;
    margin: 0.7rem 0 0;
    color: var(--ink-2);
    font-size: 0.95em;
    line-height: 1.6;
  }

  /* ── Editions ──────────────────────────────── */

  .edition {
    scroll-margin-top: 2rem;
  }

  .edition + .edition {
    margin-top: 3rem;
    padding-top: 2.5rem;
    border-top: 3px double var(--ink-2);
  }

  .dateline {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4em 1.1em;
    align-items: baseline;
    font-family: var(--font-code);
    font-size: 0.7rem;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }

  .ed-label {
    color: var(--ink-3);
  }

  .dateline time {
    color: var(--ink);
    font-weight: 650;
  }

  .ed-category {
    margin-left: auto;
    color: var(--accent-ink);
    font-weight: 700;
  }

  .edition h2 {
    max-width: 24ch;
    margin: 0.8rem 0 0;
    font-family: var(--font-display);
    font-size: clamp(1.9rem, 3.2vw, 2.8rem);
    font-weight: 500;
    letter-spacing: -0.03em;
    line-height: 1.08;
  }

  .deck {
    max-width: 40em;
    margin: 1rem 0 1.9rem;
    color: var(--ink-2);
    font-size: 1.05rem;
    line-height: 1.65;
  }

  .edition-body.has-briefs {
    display: grid;
    grid-template-columns: minmax(0, 2fr) minmax(11em, 1fr);
    gap: clamp(1.5em, 3vw, 2.5em);
  }

  .story + .story {
    margin-top: 1.4em;
  }

  .story h3 {
    margin: 0 0 0.35em;
    font-family: var(--font-display);
    font-size: 1.1rem;
    font-weight: 600;
  }

  .story p {
    margin: 0;
    color: var(--ink-2);
    font-size: 0.9rem;
    line-height: 1.65;
  }

  .briefs {
    padding-left: clamp(1em, 2vw, 1.5em);
    border-left: 1px solid var(--line);
  }

  .briefs-label {
    margin: 0 0 0.9em;
    color: var(--ink-3);
    font-size: 0.65rem;
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .brief + .brief {
    margin-top: 1em;
  }

  .brief h4 {
    margin: 0 0 0.25em;
    font-size: 0.82rem;
    font-weight: 650;
  }

  .brief p {
    margin: 0;
    color: var(--ink-2);
    font-size: 0.8rem;
    line-height: 1.55;
  }

  .folio-line {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5em 0.9em;
    align-items: baseline;
    margin-top: 2rem;
    padding-top: 1rem;
    border-top: 1px solid var(--line);
    font-family: var(--font-code);
    font-size: 0.72rem;
  }

  .filed {
    color: var(--ink-3);
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }

  .folio-tags {
    display: inline-flex;
    flex-wrap: wrap;
    gap: 0.3em 0.4em;
    align-items: baseline;
  }

  .folio-tags a {
    color: var(--ink-2);
    text-decoration: none;
  }

  .folio-tags a + a::before {
    padding-right: 0.4em;
    color: var(--ink-3);
    content: "·";
  }

  .folio-tags a:hover {
    color: var(--accent-ink);
  }

  .source-link {
    margin-left: auto;
    color: var(--accent-ink);
    font-weight: 650;
    text-decoration: none;
  }

  .source-link:hover {
    text-decoration: underline;
    text-underline-offset: 0.2em;
  }

  /* ── States ────────────────────────────────── */

  .state {
    display: grid;
    gap: 0.5em;
    padding: 2em;
    border: 1px solid var(--line);
    border-radius: var(--radius);
    background: color-mix(in srgb, var(--card-bg) 88%, transparent);
  }

  .state span {
    color: var(--ink-2);
  }

  .empty-meta {
    display: flex;
    flex-wrap: wrap;
    gap: 0.65em 1.1em;
    align-items: center;
    color: var(--ink-3);
    font-family: var(--font-code);
    font-size: 0.68em;
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
    max-width: 18ch;
    margin: 1.2rem 0 0;
    font-family: var(--font-display);
    font-size: clamp(1.9rem, 3vw, 2.6rem);
    font-weight: 500;
    letter-spacing: -0.03em;
    line-height: 1.08;
  }

  .empty-entry > p {
    max-width: 38em;
    margin: 1rem 0 0;
    color: var(--ink-2);
    font-size: 0.98em;
    line-height: 1.7;
  }

  .empty-flow {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    margin-top: 2.5em;
    border-top: 1px solid var(--line);
    border-bottom: 1px solid var(--line);
  }

  .empty-flow > div {
    display: grid;
    align-content: start;
    min-width: 0;
    padding: 1.25em 1em 1.35em 0;
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
    margin-top: 1.4em;
    font-family: var(--font-display);
    font-size: 1em;
    font-weight: 600;
  }

  .empty-flow small {
    margin-top: 0.35em;
    color: var(--ink-3);
    font-size: 0.68em;
    line-height: 1.45;
  }

  /* ── Mobile ────────────────────────────────── */

  @media (max-width: 760px) {
    .updates-page {
      padding: 2.5rem 1.35em 4em;
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

    .spine-brand {
      display: flex;
      gap: 1em;
      align-items: baseline;
      justify-content: space-between;
      padding-bottom: 1em;
      border-bottom: 1px solid var(--line);
    }

    .spine-sub {
      margin: 0;
    }

    .nameplate {
      margin-top: 1.4rem;
    }

    /* Filters flatten into wrapping inline rows so they cost three or four
       lines, not a column. Touch height comes from the padding. */
    .spine-filters {
      margin-top: 1.2rem;
    }

    .spine-list {
      display: flex;
      flex-wrap: wrap;
      gap: 0 1.1em;
      margin-bottom: 0.8rem;
    }

    .spine-list a {
      padding: 0.62em 0;
      border-left: 0;
      border-bottom: 2px solid transparent;
    }

    .spine-list a.active {
      border-bottom-color: var(--accent-ink);
    }

    .filter-results {
      padding-left: 0;
    }

    .date-rail {
      position: sticky;
      z-index: 4;
      top: 0;
      display: flex;
      gap: 1.5em;
      align-items: baseline;
      margin: 0 -1.35em 2.2rem;
      padding: 0.6em 1.35em;
      overflow-x: auto;
      border-bottom: 1px solid var(--line);
      background: color-mix(in srgb, var(--paper) 92%, transparent);
      backdrop-filter: blur(14px);
    }

    .date-rail > .spine-label {
      display: none;
    }

    .rail-month,
    .rail-month + .rail-month {
      display: flex;
      gap: 0.65em;
      align-items: center;
      margin: 0;
    }

    .rail-month > p {
      min-width: max-content;
      margin: 0 0.3em 0 0;
      padding-left: 0;
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

    .edition-body.has-briefs {
      grid-template-columns: 1fr;
    }

    .briefs {
      margin-top: 1.6em;
      padding-top: 1.2em;
      padding-left: 0;
      border-top: 1px solid var(--line);
      border-left: 0;
    }

    .source-link {
      margin-left: 0;
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
