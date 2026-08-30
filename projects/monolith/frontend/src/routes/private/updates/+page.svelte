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

<main class={`updates-page shell ${period}`}>
  <header class="masthead">
    <div class="masthead-label">
      <p class="kicker">Private release journal</p>
      <span>Daily editions</span>
    </div>

    <div class="masthead-copy">
      <h1>Product updates</h1>
      <p class="intro">
        What became possible across the homelab, with the exact source range
        behind every update.
      </p>

      <div class="facets" aria-label="Filter product updates">
        {#if data.projects.length}
          <div class="facet-row">
            <span class="facet-label">Projects</span>
            <div class="facet-options">
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
                  >{label(facet.value)}
                  <span>{facet.count}</span
                  >{#if data.selectedProject === facet.value}<span
                      class="remove"
                      aria-hidden="true">×</span
                    >{/if}</a
                >
              {/each}
            </div>
          </div>
        {/if}
        {#if data.technologies.length}
          <div class="facet-row">
            <span class="facet-label">Technologies</span>
            <div class="facet-options">
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
                  >{label(facet.value)}
                  <span>{facet.count}</span
                  >{#if data.selectedTechnology === facet.value}<span
                      class="remove"
                      aria-hidden="true">×</span
                    >{/if}</a
                >
              {/each}
            </div>
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
            <a class="clear" href="/updates">Clear filters</a>
          </p>
        {/if}
      </div>
    </div>
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
      <div class="empty-journal">
        <aside class="empty-rail" aria-hidden="true">
          <span>Archive</span>
          <strong>00</strong>
          <small>editions</small>
        </aside>

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
      </div>
    {/if}
  {:else}
    <div class="journal-layout">
      <nav class="date-rail" aria-label="Updates by date">
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

      <section class="entries" aria-label="Product update entries">
        {#each data.updates as update}
          <article
            id={`update-${update.published_on}`}
            data-update-date={update.published_on}
          >
            <div class="entry-meta">
              <time datetime={update.published_on}
                >{formatDate(update.published_on)}</time
              >
              <span class={`category category-${update.category}`}
                >{label(update.category)}</span
              >
            </div>

            <h2>{update.headline}</h2>
            <p class="summary">{update.summary}</p>

            <div class="section-block">
              <h3>What is new</h3>
              <div class="items">
                {#each update.highlights as item}
                  <div class="item">
                    <h4>{item.title}</h4>
                    <p>{item.description}</p>
                  </div>
                {/each}
              </div>
            </div>

            {#if update.improvements.length}
              <div class="section-block supporting">
                <h3>Also improved</h3>
                <div class="items">
                  {#each update.improvements as item}
                    <div class="item">
                      <h4>{item.title}</h4>
                      <p>{item.description}</p>
                    </div>
                  {/each}
                </div>
              </div>
            {/if}

            <footer class="entry-footer">
              <div class="tags" aria-label="Projects and technologies">
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
                    class="technology"
                    href={facetHref(
                      "technology",
                      technology,
                      data.selectedProject,
                      data.selectedTechnology,
                    )}>{label(technology)}</a
                  >
                {/each}
              </div>
              <a
                class="source-link"
                href={update.source_compare_url}
                target="_blank"
                rel="noreferrer"
                >Explore {update.source_commit_count}
                {update.source_commit_count === 1 ? "commit" : "commits"} on GitHub
                <span aria-hidden="true">&nearr;</span></a
              >
            </footer>
          </article>
        {/each}
      </section>
    </div>
  {/if}
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
    min-height: 100vh;
    box-sizing: border-box;
    padding: clamp(3.5rem, 6vh, 5rem) clamp(1.5em, 6vw, 5.5em) 8em;
    color: var(--ink);
    background:
      radial-gradient(circle at 85% 4%, var(--glow-a), transparent 27em),
      radial-gradient(circle at 8% 24%, var(--glow-b), transparent 24em),
      var(--paper);
    font-family: var(--font-ui);
    font-size: 16px;
  }

  .masthead {
    display: grid;
    grid-template-columns: minmax(9em, 13em) minmax(0, 52em);
    gap: clamp(2em, 6vw, 7em);
    max-width: 76em;
    margin: 0 auto 3rem;
  }

  .kicker {
    margin: 0;
    color: var(--accent);
    font-size: 0.72em;
    font-weight: 700;
    letter-spacing: 0.15em;
    text-transform: uppercase;
  }

  .masthead-label {
    padding-top: 0.55em;
  }

  .masthead-label > span {
    display: block;
    margin-top: 0.55em;
    color: var(--ink-3);
    font-family: var(--font-code);
    font-size: 0.68em;
  }

  .masthead-copy {
    min-width: 0;
  }

  h1 {
    max-width: 11ch;
    margin: 0;
    font-family: var(--font-display);
    font-size: clamp(2.6rem, 5.5vw, 4.5rem);
    font-weight: 450;
    letter-spacing: -0.05em;
    line-height: 0.94;
  }

  .intro {
    max-width: 38em;
    margin: 1.25rem 0 0;
    color: var(--ink-2);
    font-size: clamp(1em, 1.7vw, 1.15em);
    line-height: 1.65;
  }

  .facets {
    display: grid;
    gap: 0.85em;
    margin-top: 1.75rem;
    padding-top: 1.35em;
    border-top: 1px solid var(--line);
  }

  .facet-row {
    display: grid;
    grid-template-columns: 6.5em 1fr;
    gap: 1em;
    align-items: baseline;
  }

  .facet-label {
    color: var(--ink-3);
    font-size: 0.7em;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .facet-options {
    display: flex;
    flex-wrap: wrap;
    gap: 0.35em 0.95em;
  }

  .facet-options a,
  .clear {
    color: var(--ink-2);
    text-decoration: none;
  }

  .facet-options a {
    padding: 0.3rem 0.7rem;
    border: 1px solid var(--line);
    border-radius: 999px;
    background: transparent;
    font-size: 0.8em;
  }

  .facet-options a span {
    font-family: var(--font-code);
  }

  .facet-options a:hover,
  .clear:hover {
    color: var(--accent);
  }

  .facet-options a:hover {
    border-color: var(--accent);
  }

  .facet-options a.active {
    /* Mixed toward ink so the paper-colored label clears WCAG AA in every
       period palette; bare var(--accent) fails at 3.85:1 in dawn. */
    border-color: color-mix(in srgb, var(--accent) 85%, var(--ink));
    color: var(--paper);
    background: color-mix(in srgb, var(--accent) 85%, var(--ink));
    font-weight: 650;
  }

  .facet-options .remove {
    margin-left: 0.15rem;
  }

  .filter-results {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem 0.6rem;
    align-items: baseline;
    margin: 0;
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.72rem;
  }

  .clear {
    border-bottom: 1px solid currentColor;
  }

  .updates-page a:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 3px;
  }

  .journal-layout {
    display: grid;
    grid-template-columns: minmax(9em, 13em) minmax(0, 52em);
    gap: clamp(2em, 6vw, 7em);
    justify-content: center;
    max-width: 76em;
    margin: 0 auto;
  }

  .date-rail {
    position: sticky;
    top: 5em;
    align-self: start;
    max-height: calc(100vh - 7em);
    overflow-y: auto;
    scrollbar-width: none;
  }

  .rail-month + .rail-month {
    margin-top: 1.6em;
  }

  .rail-month > p {
    margin: 0 0 0.6em;
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
    color: var(--ink-3);
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
    color: var(--accent);
  }

  .rail-month a.active > span {
    font-weight: 700;
  }

  .rail-month a.active {
    border-left-color: var(--accent);
  }

  .entries article {
    scroll-margin-top: 3em;
    padding-bottom: 3.5rem;
  }

  .entries article + article {
    padding-top: 3.5rem;
    border-top: 1px solid var(--line);
  }

  .entry-meta {
    display: flex;
    flex-wrap: wrap;
    gap: 0.65em 1.1em;
    align-items: center;
    margin-bottom: 1.25em;
    color: var(--ink-3);
    font-family: var(--font-code);
    font-size: 0.7em;
    letter-spacing: 0.02em;
  }

  .entry-meta time {
    color: var(--ink-2);
  }

  .category {
    padding: 0.25em 0.5em;
    border: 1px solid var(--line);
    border-radius: 999px;
    color: var(--accent);
    font-family: var(--font-ui);
    font-size: 0.62em;
    font-weight: 700;
    letter-spacing: 0.07em;
    text-transform: uppercase;
  }

  h2 {
    max-width: 26ch;
    margin: 0;
    font-family: var(--font-display);
    font-size: clamp(1.7rem, 3vw, 2.6rem);
    font-weight: 500;
    letter-spacing: -0.035em;
    line-height: 1.1;
  }

  .summary {
    max-width: 42em;
    margin: 1rem 0 2.25rem;
    color: var(--ink-2);
    font-size: clamp(1em, 2vw, 1.2em);
    line-height: 1.7;
  }

  .section-block {
    display: grid;
    grid-template-columns: minmax(7em, 10em) 1fr;
    gap: clamp(1em, 4vw, 3em);
    padding: 1.6em 0;
    border-top: 1px solid var(--line);
  }

  .section-block h3 {
    margin: 0;
    color: var(--accent);
    font-size: 0.7em;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }

  .items {
    display: grid;
    gap: 1.5em;
  }

  .item h4 {
    margin: 0 0 0.4em;
    font-family: var(--font-display);
    font-size: 1.15em;
    font-weight: 600;
  }

  .item p {
    margin: 0;
    color: var(--ink-2);
    font-size: 0.9em;
    line-height: 1.65;
  }

  .supporting h3 {
    color: var(--ink-3);
  }

  .entry-footer {
    display: grid;
    gap: 1.5em;
    margin-top: 2.25em;
    padding-top: 1.5em;
    border-top: 1px solid var(--line);
  }

  .tags {
    display: flex;
    flex-wrap: wrap;
    gap: 0.45em;
  }

  .tags a {
    padding: 0.3em 0.55em;
    border-radius: 999px;
    color: var(--ink-2);
    background: color-mix(in srgb, var(--ink) 5%, transparent);
    font-size: 0.68em;
    text-decoration: none;
  }

  .tags a.technology {
    border: 1px solid var(--line);
    background: transparent;
  }

  .tags a:hover {
    color: var(--accent);
  }

  .source-link {
    width: max-content;
    max-width: 100%;
    color: var(--accent);
    font-size: 0.78em;
    font-weight: 650;
    text-decoration: none;
  }

  .source-link:hover {
    text-decoration: underline;
    text-underline-offset: 0.2em;
  }

  .state {
    display: grid;
    gap: 0.5em;
    max-width: 42em;
    margin: 2em auto;
    padding: 2em;
    border: 1px solid var(--line);
    border-radius: var(--radius);
    background: color-mix(in srgb, var(--card-bg) 88%, transparent);
  }

  .state span {
    color: var(--ink-2);
  }

  .empty-journal {
    display: grid;
    grid-template-columns: minmax(9em, 13em) minmax(0, 52em);
    gap: clamp(2em, 6vw, 7em);
    max-width: 76em;
    margin: 0 auto;
  }

  .empty-rail {
    display: grid;
    align-content: start;
    padding-top: 1.25em;
    border-top: 1px solid var(--line);
  }

  .empty-rail span,
  .empty-rail small {
    color: var(--ink-3);
    font-size: 0.65em;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }

  .empty-rail strong {
    margin-top: 1em;
    color: var(--ink-3);
    font-family: var(--font-display);
    font-size: 3.5em;
    font-weight: 450;
    letter-spacing: -0.05em;
    line-height: 0.9;
  }

  .empty-rail small {
    margin-top: 0.45em;
    font-family: var(--font-code);
    font-size: 0.58em;
    font-weight: 500;
  }

  .empty-entry {
    min-width: 0;
    padding-top: 1.25em;
    border-top: 1px solid var(--line);
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
    max-width: 14ch;
    margin-top: 2.1em;
    font-size: clamp(2.4em, 5vw, 4.25em);
  }

  .empty-entry > p {
    max-width: 38em;
    margin: 1.35em 0 0;
    color: var(--ink-2);
    font-size: 0.98em;
    line-height: 1.7;
  }

  .empty-flow {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    margin-top: 3.25em;
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
    color: var(--accent);
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

  @media (max-width: 760px) {
    .updates-page {
      padding: 3.5rem 1.35em 5em;
    }

    .masthead {
      display: block;
      margin-bottom: 3em;
    }

    .masthead-label {
      display: flex;
      justify-content: space-between;
      gap: 1em;
      align-items: baseline;
      padding: 0 0 1em;
      border-bottom: 1px solid var(--line);
    }

    .masthead-label > span {
      margin: 0;
    }

    h1 {
      margin-top: 1.65em;
    }

    .facet-row {
      grid-template-columns: 1fr;
      gap: 0.35em;
    }

    .journal-layout {
      display: block;
    }

    .empty-journal {
      display: block;
    }

    .empty-rail {
      display: none;
    }

    .empty-entry h2 {
      margin-top: 1.65em;
    }

    .empty-flow {
      grid-template-columns: 1fr;
      margin-top: 2.5em;
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

    .date-rail {
      z-index: 4;
      top: 0;
      display: flex;
      gap: 1.5em;
      max-height: none;
      margin: 0 -1.25em 3.5em;
      padding: 0.8em 1.25em;
      overflow-x: auto;
      border-bottom: 1px solid var(--line);
      background: color-mix(in srgb, var(--paper) 92%, transparent);
      backdrop-filter: blur(14px);
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
    }

    .rail-month a {
      display: block;
      min-width: 1.7em;
      padding: 0.3em;
      text-align: center;
      /* In the horizontal strip the desktop left bar would read as a
         divider between pills, so the marker moves to an underline. */
      border-left: 0;
      border-bottom: 2px solid transparent;
    }

    .rail-month a.active {
      border-bottom-color: var(--accent);
    }

    .rail-month small {
      display: none;
    }

    .section-block {
      grid-template-columns: 1fr;
    }
  }
</style>
