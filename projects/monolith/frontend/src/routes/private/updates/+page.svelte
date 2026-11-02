<script>
  import "$lib/public/styles/technical-drawing.css";
  import TechnicalDrawingChrome from "$lib/public/components/TechnicalDrawingChrome.svelte";
  import Trail from "$lib/public/components/Trail.svelte";
  import {
    facetHref,
    formatDate,
    label,
    monthHref,
    monthLabel,
  } from "./updates.js";

  let { data } = $props();
  let activeDate = $state("");
  let activeMonth = $derived(activeDate.slice(0, 7) || data.selectedMonth);
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

{#snippet trail()}
  <Trail
    root={{ label: "dashboard", href: "/" }}
    crumbs={[{ label: "updates" }]}
  />
{/snippet}

<TechnicalDrawingChrome {trail} />
<main class="td sheet updates-page">
  <div class="frame">
    <h1 class="sr-only">Product updates</h1>

    <div class="journal">
      <aside class="spine">
        <div class="trail-dock">{@render trail()}</div>
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
                      data.selectedMonth,
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
                      data.selectedMonth,
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
          {#if filtering && !data.error && !data.invalidMonth}
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
              <a
                class="clear"
                href={data.selectedMonth
                  ? monthHref(data.selectedMonth, "", "")
                  : "/updates"}>Clear</a
              >
            </p>
          {/if}
        </nav>

        {#if data.months.length}
          <nav class="date-rail" aria-label="Updates by date">
            <p class="sec-label">/ Index</p>
            {#each data.months as group}
              <div class="rail-month">
                <details open={group.month === activeMonth}>
                  <summary>
                    <span
                      class="month-link"
                      aria-current={group.month === data.selectedMonth
                        ? "page"
                        : undefined}
                    >
                      <span class="month-name">{monthLabel(group.month)}</span>
                      <span class="group-count">{group.count}</span>
                    </span>
                  </summary>
                  <div class="rail-days">
                    {#each group.editions as update}
                      <a
                        class:active={activeDate === update.published_on}
                        href={monthHref(
                          group.month,
                          data.selectedProject,
                          data.selectedTechnology,
                          update.published_on,
                        )}
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
                {#if group.month !== data.selectedMonth}
                  <a
                    class="month-navigation"
                    aria-label={`Open ${monthLabel(group.month)}`}
                    href={monthHref(
                      group.month,
                      data.selectedProject,
                      data.selectedTechnology,
                    )}>Open</a
                  >
                {/if}
              </div>
            {/each}
          </nav>
        {/if}
      </aside>

      <div class="content">
        {#if data.invalidMonth}
          <div class="state" role="alert">
            <strong>That journal month is not valid.</strong>
            <span>
              <a class="clear" href="/updates"
                >Open the newest available month.</a
              >
            </span>
          </div>
        {:else if data.error}
          <div class="state" role="alert">
            <strong>The journal is unavailable.</strong>
            <span>The archive service did not respond. Try again shortly.</span>
          </div>
        {:else if !data.updates.length}
          {#if filtering}
            <div class="state">
              {#if data.months.length}
                <strong>No updates match these filters in this month.</strong>
                <span>Choose another month or clear a filter.</span>
              {:else}
                <strong>No updates match these filters.</strong>
                <span>Clear a filter to see the complete journal.</span>
              {/if}
            </div>
          {:else if data.months.length}
            <div class="state">
              <strong>No updates were published in this month.</strong>
              <span>
                <a class="clear" href={monthHref(data.months[0].month, "", "")}
                  >Open the newest available month.</a
                >
              </span>
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
          <section class="editions ruled" aria-label="Product update entries">
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
                </p>

                <div class="ed-lead">
                  <h2>{update.headline}</h2>
                  <p class="summary">{update.summary}</p>
                </div>

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
                            data.selectedMonth,
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
                            data.selectedMonth,
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
                      {update.source_commit_count === 1 ? "commit" : "commits"} on
                      GitHub
                      <span aria-hidden="true">&nearr;</span></a
                    >
                  </p>
                </div>

                <div class="ed-section">
                  <p class="sec-sublabel">What is new</p>
                  <div class="items">
                    {#each update.highlights as item}
                      <div class="item">
                        <h3>{item.title}</h3>
                        <p>{item.description}</p>
                      </div>
                    {/each}
                  </div>
                </div>

                {#if update.improvements.length}
                  <div class="ed-section">
                    <p class="sec-sublabel">Also improved</p>
                    <div class="items supporting">
                      {#each update.improvements as item}
                        <div class="item">
                          <h3>{item.title}</h3>
                          <p>{item.description}</p>
                        </div>
                      {/each}
                    </div>
                  </div>
                {/if}
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
    /* Keep journal navigation smooth without changing other sheet routes. */
    :global(html:has(main.updates-page)) {
      scroll-behavior: smooth;
    }
  }

  :global(body) {
    margin: 0;
  }

  /* The journal's 48em measure and all rem spacing use a 16px basis.
     The shared sheet owns the overscroll ground and scheme precedence. */
  :global(html:has(main.updates-page)) {
    font-size: 16px;
  }

  .updates-page {
    /* Accent mixed toward ink: one darker accent for all small chrome so
       accent text sits well above AA on both schemes (6.1:1 day, 8.8:1
       night) instead of hovering near the floor. */
    --accent-ink: color-mix(in srgb, var(--accent) 85%, var(--ink));
    padding: clamp(3.25rem, 5vh, 3.75rem) clamp(1.5em, 5vw, 4.5em) 6em;
    font-size: 16px;
  }
  .updates-page :global(:focus-visible) {
    /* Keep the inherited public-reset focus shape during this migration. */
    border-radius: calc(var(--radius) * 0.75);
  }
  .updates-page .ed-head {
    display: block;
    gap: normal;
    color: var(--ink);
  }

  .updates-page .frame {
    /* Sheet width = spine + gap + a 48em text measure: the ruled strokes
       end where the text ends, so no rule implies content that is not
       there. */
    max-width: 69em;
  }

  /* ── Layout ── */

  .updates-page .journal {
    grid-template-columns: minmax(12em, 15em) minmax(0, 48em);
  }

  /* ── Spine: filters ── */

  .updates-page .spine-group summary {
    display: flex;
    gap: 0.6em;
    align-items: baseline;
    justify-content: space-between;
    padding: 0.5em 0;
    border-bottom: 1px solid var(--line);
    cursor: pointer;
    list-style: none;
  }

  .updates-page .spine-group summary::-webkit-details-marker {
    display: none;
  }

  .updates-page .group-name {
    /* Private filter headings retain their sentence-case UI typography. */
    font-family: var(--font-ui);
    color: inherit;
    font-size: 0.8em;
    font-weight: 600;
    letter-spacing: normal;
    text-transform: none;
  }

  .updates-page .spine-group summary:hover .group-name {
    color: var(--accent-ink);
  }

  .updates-page .spine-group[open] .group-count::after,
  .updates-page .rail-month > details[open] .group-count::after {
    content: "\2212";
  }

  .updates-page .spine-list {
    padding: 0.35em 0;
    border-bottom: 1px solid var(--line);
  }

  .updates-page .spine-list a {
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

  .updates-page .spine-list a:hover,
  .updates-page .spine-list a.active {
    color: var(--accent-ink);
  }

  .updates-page .spine-list a.active {
    border-left-color: var(--accent-ink);
    font-weight: 650;
  }

  .updates-page .spine-list .count {
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.85em;
  }

  .updates-page .spine-list .remove {
    margin-left: 0.2em;
  }

  .updates-page .spine-list a.active .count {
    color: inherit;
  }

  .updates-page .filter-results {
    display: flex;
    flex-wrap: wrap;
    gap: 0.3rem 0.6rem;
    align-items: baseline;
    margin: 0.8em 0 0;
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.72rem;
  }

  .updates-page .clear {
    color: var(--ink-2);
    border-bottom: 1px solid currentColor;
    text-decoration: none;
  }

  .updates-page .clear:hover {
    color: var(--accent-ink);
  }

  /* ── Spine: index ── */

  .updates-page .date-rail {
    margin-top: 1.8rem;
  }

  .updates-page .rail-month summary {
    padding: 0 3.2rem 0 0;
    border-bottom: 1px solid var(--line);
    cursor: pointer;
    list-style: none;
  }

  .updates-page .rail-month summary::-webkit-details-marker {
    display: none;
  }

  .updates-page .rail-month .month-link {
    display: flex;
    gap: 0.6em;
    align-items: baseline;
    justify-content: space-between;
    padding: 0.5em 0;
    color: inherit;
    text-decoration: none;
  }

  .updates-page .rail-month {
    position: relative;
  }

  .updates-page .month-navigation {
    position: absolute;
    top: 0;
    right: 0;
    padding: 0.58em 0;
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.62em;
    text-decoration: none;
    text-transform: uppercase;
  }

  .updates-page .month-navigation:hover {
    color: var(--accent-ink);
  }

  .updates-page .month-name {
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.68em;
    font-weight: 500;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .updates-page .rail-month summary:hover .month-name {
    color: var(--accent-ink);
  }

  .updates-page .rail-days {
    padding: 0.35em 0;
    border-bottom: 1px solid var(--line);
  }

  .updates-page .rail-days a {
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

  .updates-page .rail-days a > span {
    font-family: var(--font-code);
    font-size: 0.75em;
  }

  .updates-page .rail-month small {
    display: -webkit-box;
    overflow: hidden;
    font-size: 0.7em;
    line-height: 1.3;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
  }

  .updates-page .rail-days a:hover,
  .updates-page .rail-days a.active {
    color: var(--accent-ink);
  }

  .updates-page .rail-days a.active {
    border-left-color: var(--accent-ink);
  }

  .updates-page .rail-days a.active > span {
    font-weight: 700;
  }

  /* ── Editions ── */

  .updates-page .ed-head time {
    color: var(--ink-2);
  }

  .updates-page .meta-rows {
    margin: 0;
    border-top: 1px solid var(--stroke);
  }

  /* Spec-table shape: fixed label column, value flows left beside it.
     The spine's space-between works for short counts; a long value list
     right-aligned against it wraps ragged with a gulf in the middle. */
  .updates-page .meta-kv {
    display: grid;
    grid-template-columns: 6.5em minmax(0, 1fr);
    align-items: stretch;
    margin: 0;
  }

  .updates-page .meta-kv + .meta-kv {
    border-top: 1px solid var(--line);
  }

  .updates-page .meta-kv .k {
    padding: 0.5em 0 0.5em 1rem;
  }

  .updates-page .meta-kv .v {
    padding: 0.5em 1rem 0.5em 1.2em;
    /* Label column rule, drawn by the value cell so it spans row height. */
    border-left: 1px solid var(--line);
  }

  .updates-page .meta-kv .v.filed-tags {
    padding: 0;
  }

  .updates-page .meta-kv .k,
  .updates-page .meta-kv .v {
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.72rem;
  }

  /* A real table of uniform cells: flex with a fixed basis sizes every
     cell the same, each cell draws its own left and top rule, and the
     container clips the outer edges so only internal dividers remain. */
  .updates-page .filed-tags {
    display: flex;
    flex-wrap: wrap;
    overflow: hidden;
  }

  .updates-page .filed-tags a {
    box-sizing: border-box;
    flex: 0 0 10.5rem;
    margin: -1px 0 0 -1px;
    padding: 0.35em 0.7em;
    border-top: 1px solid var(--line);
    border-left: 1px solid var(--line);
    color: var(--ink-2);
    text-decoration: none;
  }

  .updates-page .filed-tags a:hover {
    color: var(--accent-ink);
  }

  .updates-page .source-link {
    color: var(--ink-2);
    text-decoration: none;
  }

  .updates-page .source-link:hover {
    color: var(--accent-ink);
  }

  .updates-page .ed-lead h2 {
    max-width: 40ch;
    margin: 0;
    font-size: clamp(1.1rem, 1.6vw, 1.35rem);
    font-weight: 700;
    letter-spacing: -0.02em;
    line-height: 1.15;
  }

  .updates-page .summary {
    margin: 0.5rem 0 0;
    color: var(--ink);
    font-size: 0.84rem;
    line-height: 1.5;
  }

  .updates-page .ed-section {
    border-top: 1px solid var(--stroke);
  }

  /* Tinted band distinguishes section headers from content at a glance. */
  .updates-page .sec-sublabel {
    margin: 0;
    padding: 0.45em 1rem;
    border-bottom: 1px solid var(--line);
    background: var(--band);
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.72rem;
    font-weight: 500;
  }

  .updates-page .items {
    margin: 0;
  }

  /* Items are ruled rows inside the section, not floating text. */
  .updates-page .item {
    padding: 0.6rem 1rem 0.7rem;
  }

  .updates-page .item + .item {
    border-top: 1px solid var(--line);
  }

  .updates-page .item h3 {
    margin: 0 0 0.15em;
    font-size: 0.8rem;
    font-weight: 650;
  }

  .updates-page .item p {
    margin: 0;
    color: var(--ink-2);
    font-size: 0.78rem;
    line-height: 1.5;
  }

  .updates-page .supporting .item h3 {
    font-size: 0.78rem;
  }

  .updates-page .supporting .item p {
    font-size: 0.76rem;
  }

  /* ── States ── */

  .updates-page .state {
    display: grid;
    gap: 0.5em;
    padding: 1.5em;
    border: 1px solid var(--stroke);
  }

  .updates-page .state span {
    color: var(--ink-2);
  }

  .updates-page .empty-meta {
    display: flex;
    flex-wrap: wrap;
    gap: 0.65em 1.1em;
    align-items: center;
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.68em;
    text-transform: uppercase;
  }

  .updates-page .empty-meta span:last-child {
    margin-left: auto;
  }

  .updates-page .ready-dot {
    width: 0.45em;
    height: 0.45em;
    border-radius: 50%;
    background: var(--ok);
    box-shadow: 0 0 0 0.2em color-mix(in srgb, var(--ok) 13%, transparent);
  }

  .updates-page .empty-entry h2 {
    max-width: 22ch;
    margin: 1.2rem 0 0;
    font-size: clamp(1.5rem, 2.6vw, 2.2rem);
    font-weight: 700;
    letter-spacing: -0.025em;
    line-height: 1.12;
  }

  .updates-page .empty-entry > p {
    margin: 1rem 0 0;
    color: var(--ink-2);
    font-size: 0.95em;
    line-height: 1.65;
  }

  .updates-page .empty-flow {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    margin-top: 2.2em;
    border-top: 1px solid var(--stroke);
    border-bottom: 1px solid var(--stroke);
  }

  .updates-page .empty-flow > div {
    display: grid;
    align-content: start;
    min-width: 0;
    padding: 1.1em 1em 1.2em 0;
  }

  .updates-page .empty-flow > div + div {
    padding-left: 1em;
    border-left: 1px solid var(--line);
  }

  .updates-page .empty-flow span {
    color: var(--accent-ink);
    font-family: var(--font-code);
    font-size: 0.62em;
  }

  .updates-page .empty-flow strong {
    margin-top: 1.2em;
    font-size: 0.92em;
    font-weight: 650;
  }

  .updates-page .empty-flow small {
    margin-top: 0.35em;
    color: var(--ink-2);
    font-size: 0.7em;
    line-height: 1.45;
  }

  /* The folio is the first thing to go when the masthead track tightens;
     a wrapped or clipped identity string is worse than a shorter one. */

  /* ── Mobile ── */

  @media (max-width: 760px) {
    /* The chrome row above carries the trail, so nothing fixed to clear. */
    .updates-page {
      padding: 1rem 1.35em 4em;
    }

    .updates-page .spine-group summary {
      padding: 0.9em 0;
    }

    .updates-page .spine-list {
      display: flex;
      flex-wrap: wrap;
      gap: 0 1.1em;
    }

    .updates-page .spine-list a {
      padding: 0.62em 0;
      border-left: 0;
      border-bottom: 2px solid transparent;
    }

    .updates-page .spine-list a.active {
      border-bottom-color: var(--accent-ink);
    }

    .updates-page .date-rail {
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
      background: color-mix(in srgb, var(--sheet) 92%, transparent);
      backdrop-filter: blur(14px);
    }

    .updates-page .date-rail > .sec-label {
      display: none;
    }

    .updates-page .rail-month {
      margin: 0;
    }

    .updates-page .rail-month summary {
      min-width: max-content;
      border-bottom: 2px solid transparent;
    }

    .updates-page .rail-month .month-link {
      padding: 0.65em 0;
    }

    .updates-page .rail-days {
      display: flex;
      gap: 0.35em;
      align-items: center;
      padding: 0;
      border-bottom: 0;
    }

    .updates-page .rail-days a {
      display: block;
      min-width: 2.2em;
      padding: 0.65em 0.4em;
      text-align: center;
      /* In the horizontal strip the desktop left bar would read as a
         divider between pills, so the marker moves to an underline. */
      border-left: 0;
      border-bottom: 2px solid transparent;
    }

    .updates-page .rail-days a.active {
      border-bottom-color: var(--accent-ink);
    }

    .updates-page .rail-month small {
      display: none;
    }

    /* Anchor jumps must clear the sticky index strip (about 52px). */
    .updates-page .edition {
      scroll-margin-top: 4rem;
    }

    .updates-page .empty-flow {
      grid-template-columns: 1fr;
    }

    .updates-page .empty-flow > div {
      grid-template-columns: 2.5em 1fr;
      padding: 1em 0;
    }

    .updates-page .empty-flow > div + div {
      padding-left: 0;
      border-top: 1px solid var(--line);
      border-left: 0;
    }

    .updates-page .empty-flow strong,
    .updates-page .empty-flow small {
      grid-column: 2;
    }

    .updates-page .empty-flow strong {
      margin-top: 0;
    }
  }
</style>
