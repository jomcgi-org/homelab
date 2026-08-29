<script>
  import { onMount } from "svelte";
  import {
    facetHref,
    formatDate,
    formatVersion,
    groupUpdatesByMonth,
    label,
  } from "./updates.js";

  let { data } = $props();
  let activeDate = $state("");
  let monthGroups = $derived(groupUpdatesByMonth(data.updates));
  let filtering = $derived(
    Boolean(data.selectedProject || data.selectedTechnology),
  );

  $effect(() => {
    const dates = data.updates.map((update) => update.published_on);
    if (!dates.includes(activeDate)) activeDate = dates[0] ?? "";
  });

  onMount(() => {
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

<main class="updates-page shell day">
  <header class="masthead">
    <p class="kicker">Private release journal</p>
    <h1>Product updates</h1>
    <p class="intro">
      A daily record of what became possible across the homelab, with the exact
      source range behind every update.
    </p>

    <div class="facets" aria-label="Filter product updates">
      {#if data.projects.length}
        <div class="facet-row">
          <span class="facet-label">Projects</span>
          <div class="facet-options">
            {#each data.projects as facet}
              <a
                class:active={data.selectedProject === facet.value}
                href={facetHref(
                  "project",
                  facet.value,
                  data.selectedProject,
                  data.selectedTechnology,
                )}>{label(facet.value)} <span>{facet.count}</span></a
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
                href={facetHref(
                  "technology",
                  facet.value,
                  data.selectedProject,
                  data.selectedTechnology,
                )}>{label(facet.value)} <span>{facet.count}</span></a
              >
            {/each}
          </div>
        </div>
      {/if}
      {#if filtering}
        <a class="clear" href="/updates">Clear filters</a>
      {/if}
    </div>
  </header>

  {#if data.error}
    <div class="state" role="alert">
      <strong>The journal is unavailable.</strong>
      <span>The archive service did not respond. Try again shortly.</span>
    </div>
  {:else if !data.updates.length}
    <div class="state">
      <strong
        >{filtering
          ? "No updates match these filters."
          : "The first update is on its way."}</strong
      >
      <span
        >{filtering
          ? "Clear a filter to see the complete journal."
          : "Daily entries will appear here as soon as they are submitted."}</span
      >
    </div>
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
              <span>v{formatVersion(update.published_on)}</span>
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
  :global(html) {
    scroll-behavior: smooth;
  }

  :global(body) {
    margin: 0;
  }

  .updates-page {
    min-height: 100vh;
    box-sizing: border-box;
    padding: 7rem clamp(1.25rem, 5vw, 5.5rem) 8rem;
    color: var(--ink);
    background:
      radial-gradient(circle at 85% 4%, var(--glow-a), transparent 27rem),
      radial-gradient(circle at 8% 24%, var(--glow-b), transparent 24rem),
      var(--paper);
    font-family: var(--font-ui);
  }

  .masthead {
    max-width: 76rem;
    margin: 0 auto 5rem;
    padding-left: clamp(0rem, 17vw, 13rem);
  }

  .kicker {
    margin: 0 0 1rem;
    color: var(--accent);
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: 0.15em;
    text-transform: uppercase;
  }

  h1 {
    max-width: 12ch;
    margin: 0;
    font-family: var(--font-display);
    font-size: clamp(3.2rem, 8vw, 7rem);
    font-weight: 450;
    letter-spacing: -0.055em;
    line-height: 0.9;
  }

  .intro {
    max-width: 42rem;
    margin: 2rem 0 0;
    color: var(--ink-2);
    font-family: var(--font-display);
    font-size: clamp(1.1rem, 2vw, 1.4rem);
    line-height: 1.55;
  }

  .facets {
    display: grid;
    gap: 0.85rem;
    margin-top: 3rem;
    padding-top: 1.35rem;
    border-top: 1px solid var(--line);
  }

  .facet-row {
    display: grid;
    grid-template-columns: 6.5rem 1fr;
    gap: 1rem;
    align-items: baseline;
  }

  .facet-label {
    color: var(--ink-3);
    font-size: 0.7rem;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .facet-options {
    display: flex;
    flex-wrap: wrap;
    gap: 0.35rem 0.95rem;
  }

  .facet-options a,
  .clear {
    color: var(--ink-2);
    font-size: 0.8rem;
    text-decoration: none;
  }

  .facet-options a span {
    color: var(--ink-3);
    font-family: var(--font-code);
    font-size: 0.67rem;
  }

  .facet-options a:hover,
  .facet-options a.active,
  .clear:hover {
    color: var(--accent);
  }

  .facet-options a.active {
    font-weight: 650;
  }

  .clear {
    width: max-content;
    margin-left: 7.5rem;
    border-bottom: 1px solid currentColor;
  }

  .journal-layout {
    display: grid;
    grid-template-columns: minmax(9rem, 13rem) minmax(0, 52rem);
    gap: clamp(2rem, 6vw, 7rem);
    justify-content: center;
    max-width: 76rem;
    margin: 0 auto;
  }

  .date-rail {
    position: sticky;
    top: 5rem;
    align-self: start;
    max-height: calc(100vh - 7rem);
    overflow-y: auto;
    scrollbar-width: none;
  }

  .rail-month + .rail-month {
    margin-top: 1.6rem;
  }

  .rail-month > p {
    margin: 0 0 0.6rem;
    color: var(--ink-3);
    font-size: 0.65rem;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }

  .rail-month a {
    display: grid;
    grid-template-columns: 2rem 1fr;
    gap: 0.65rem;
    align-items: start;
    padding: 0.45rem 0;
    color: var(--ink-3);
    text-decoration: none;
    transition: color 120ms ease;
  }

  .rail-month a > span {
    font-family: var(--font-code);
    font-size: 0.75rem;
  }

  .rail-month small {
    display: -webkit-box;
    overflow: hidden;
    font-size: 0.7rem;
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

  .entries article {
    scroll-margin-top: 3rem;
    padding-bottom: 6rem;
  }

  .entries article + article {
    padding-top: 5.5rem;
    border-top: 1px solid var(--line);
  }

  .entry-meta {
    display: flex;
    flex-wrap: wrap;
    gap: 0.65rem 1.1rem;
    align-items: center;
    margin-bottom: 1.25rem;
    color: var(--ink-3);
    font-family: var(--font-code);
    font-size: 0.7rem;
    letter-spacing: 0.02em;
  }

  .entry-meta time {
    color: var(--ink-2);
  }

  .category {
    padding: 0.25rem 0.5rem;
    border: 1px solid var(--line);
    border-radius: 999px;
    color: var(--accent);
    font-family: var(--font-ui);
    font-size: 0.62rem;
    font-weight: 700;
    letter-spacing: 0.07em;
    text-transform: uppercase;
  }

  h2 {
    max-width: 16ch;
    margin: 0;
    font-family: var(--font-display);
    font-size: clamp(2.3rem, 5vw, 4.4rem);
    font-weight: 500;
    letter-spacing: -0.035em;
    line-height: 1.02;
  }

  .summary {
    max-width: 42rem;
    margin: 1.5rem 0 3.25rem;
    color: var(--ink-2);
    font-size: clamp(1rem, 2vw, 1.2rem);
    line-height: 1.7;
  }

  .section-block {
    display: grid;
    grid-template-columns: minmax(7rem, 10rem) 1fr;
    gap: clamp(1rem, 4vw, 3rem);
    padding: 1.6rem 0;
    border-top: 1px solid var(--line);
  }

  .section-block h3 {
    margin: 0;
    color: var(--accent);
    font-size: 0.7rem;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }

  .items {
    display: grid;
    gap: 1.5rem;
  }

  .item h4 {
    margin: 0 0 0.4rem;
    font-family: var(--font-display);
    font-size: 1.15rem;
    font-weight: 600;
  }

  .item p {
    margin: 0;
    color: var(--ink-2);
    font-size: 0.9rem;
    line-height: 1.65;
  }

  .supporting h3 {
    color: var(--ink-3);
  }

  .entry-footer {
    display: grid;
    gap: 1.5rem;
    margin-top: 2.25rem;
    padding-top: 1.5rem;
    border-top: 1px solid var(--line);
  }

  .tags {
    display: flex;
    flex-wrap: wrap;
    gap: 0.45rem;
  }

  .tags a {
    padding: 0.3rem 0.55rem;
    border-radius: 999px;
    color: var(--ink-2);
    background: color-mix(in srgb, var(--ink) 5%, transparent);
    font-size: 0.68rem;
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
    font-size: 0.78rem;
    font-weight: 650;
    text-decoration: none;
  }

  .source-link:hover {
    text-decoration: underline;
    text-underline-offset: 0.2rem;
  }

  .state {
    display: grid;
    gap: 0.5rem;
    max-width: 42rem;
    margin: 2rem auto;
    padding: 2rem;
    border: 1px solid var(--line);
    border-radius: var(--radius);
    background: color-mix(in srgb, var(--card-bg) 88%, transparent);
  }

  .state span {
    color: var(--ink-2);
  }

  @media (max-width: 760px) {
    .updates-page {
      padding-top: 6rem;
    }

    .masthead {
      margin-bottom: 3rem;
      padding-left: 0;
    }

    .facet-row {
      grid-template-columns: 1fr;
      gap: 0.35rem;
    }

    .clear {
      margin-left: 0;
    }

    .journal-layout {
      display: block;
    }

    .date-rail {
      z-index: 4;
      top: 0;
      display: flex;
      gap: 1.5rem;
      max-height: none;
      margin: 0 -1.25rem 3.5rem;
      padding: 0.8rem 1.25rem;
      overflow-x: auto;
      border-bottom: 1px solid var(--line);
      background: color-mix(in srgb, var(--paper) 92%, transparent);
      backdrop-filter: blur(14px);
    }

    .rail-month,
    .rail-month + .rail-month {
      display: flex;
      gap: 0.65rem;
      align-items: center;
      margin: 0;
    }

    .rail-month > p {
      min-width: max-content;
      margin: 0 0.3rem 0 0;
    }

    .rail-month a {
      display: block;
      min-width: 1.7rem;
      padding: 0.3rem;
      text-align: center;
    }

    .rail-month small {
      display: none;
    }

    .section-block {
      grid-template-columns: 1fr;
    }
  }
</style>
