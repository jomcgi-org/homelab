<script module>
  let cachedSearchIndex = null;
  let searchIndexRequest = null;

  function loadSearchIndex() {
    if (cachedSearchIndex) return Promise.resolve(cachedSearchIndex);
    if (!searchIndexRequest) {
      searchIndexRequest = fetch("/slop/factory/search-index")
        .then((response) => {
          if (!response.ok) throw new Error("record search index unavailable");
          return response.json();
        })
        .then((index) => {
          cachedSearchIndex = index;
          return index;
        })
        .catch((error) => {
          searchIndexRequest = null;
          throw error;
        });
    }
    return searchIndexRequest;
  }
</script>

<script>
  import { SchemeToggle, Seo } from "$lib/public/components";
  import "$lib/public/factory/factory.css";
  import { MARK_DEFINITIONS } from "$lib/public/factory/marks.js";
  import { markClass, paginate } from "$lib/public/factory/model.js";
  import {
    decodeSearchIndex,
    rankIndexMatches,
  } from "$lib/public/factory/search-index.js";
  import Trail from "../../Trail.svelte";

  let { data } = $props();
  let showVerified = $state(true);
  let showUnverified = $state(true);
  let showContradicted = $state(true);
  let definitionsOpen = $state(false);
  // The page is a fixed height, so the note list pages rather than scrolls.
  // Ten rows is what fits beside the sidebar at a normal desktop height with
  // the pager still visible.
  const PAGE_SIZE = 10;
  let notePage = $state(0);
  let query = $state(data.q);
  let searchIndex = $state(null);
  // Decoded once per index fetch, never per keystroke.
  const indexNotes = $derived(
    searchIndex ? decodeSearchIndex(searchIndex) : [],
  );
  let suggestionsDismissed = $state(false);
  let activeSuggestion = $state(-1);

  const PROJECT_COPY = {
    embervm: {
      lede: "Firecracker microVM sessions for agents: the control plane, brick nodes, snapshots, and the guests the drainer runs.",
      scope: "repo · environment:homelab",
    },
    monolith: {
      lede: "The FastAPI and SvelteKit suite: knowledge, chat, agent sessions, and the public tier.",
      scope: "repo · environment:homelab",
    },
    mcp: {
      lede: "Context Forge and the monolith-agents tool tier that every agent caller reaches.",
      scope: "repo · environment:homelab",
    },
    ci: {
      lede: "Bazel, BuildBuddy workflows, the ci loop, and the merge queue.",
      scope: "repo",
    },
    platform: {
      lede: "ArgoCD, Kargo promotion, Helm charts, and the GKE hub.",
      scope: "environment:homelab",
    },
    semgrep: {
      lede: "The rule engine, the scan pipeline, and the CI gate.",
      scope: "repo",
    },
    frontend: {
      lede: "SvelteKit routes, the four design systems, and the public site.",
      scope: "repo",
    },
  };

  const number = (value) => Number(value ?? 0).toLocaleString();
  const day = (value) => value?.slice(5, 10).replace("-", "·") ?? "";
  const count = (entity, state) => Number(entity.note_counts?.[state] ?? 0);
  const verifiedTotal = $derived(Number(data.facts.totals?.verified ?? 0));
  const unverifiedTotal = $derived(Number(data.facts.totals?.unverified ?? 0));
  // A fact is contradicted by appearing in a contradiction pair, not by
  // carrying a disputed flag: that column is empty across the whole corpus, so
  // filtering on it was a control that could never do anything.
  const contradictedTotal = $derived(Number(data.facts.contradictions ?? 0));
  const contradictedIds = $derived(
    new Set(
      (data.chapter?.contradictions ?? []).flatMap((pair) => [
        pair.a.note_id,
        pair.b.note_id,
      ]),
    ),
  );
  const markTotals = $derived({
    verified: verifiedTotal,
    unverified: unverifiedTotal,
    disputed: contradictedTotal,
  });
  const noneShown = $derived(
    !showVerified && !showUnverified && !showContradicted,
  );

  function passesFilter(note) {
    if (contradictedIds.has(note.note_id) || note.disputed)
      return showContradicted;
    if (note.verification_state === "verified") return showVerified;
    if (note.verification_state === "unverified") return showUnverified;
    return showContradicted;
  }
  const selected = $derived(
    data.entities.find(
      (entity) => entity.kind === "project" && entity.slug === data.entity,
    ),
  );
  const verified = $derived(
    (data.chapter?.notes ?? []).filter(
      (note) => note.verification_state === "verified" && passesFilter(note),
    ),
  );
  const unverified = $derived(
    (data.chapter?.notes ?? []).filter(
      (note) => note.verification_state === "unverified" && passesFilter(note),
    ),
  );
  const visibleResults = $derived(data.results.filter(passesFilter));
  // One paged list serves both views: a chapter's verified rows when a topic is
  // open, the matches when a query is running.
  const listRows = $derived(data.q ? visibleResults : verified);
  const pagedRows = $derived(paginate(listRows, notePage, PAGE_SIZE));
  const instantResults = $derived(
    indexNotes.length && !suggestionsDismissed
      ? rankIndexMatches(indexNotes, query, 20)
      : [],
  );
  const activeDescendant = $derived(
    activeSuggestion >= 0 && activeSuggestion < instantResults.length
      ? `factory-search-option-${activeSuggestion}`
      : undefined,
  );
  const lastObserved = $derived(
    (data.chapter?.notes ?? [])
      .map((note) => note.observed_at)
      .filter(Boolean)
      .sort()
      .at(-1),
  );

  $effect(() => {
    let active = true;
    loadSearchIndex()
      .then((index) => {
        if (active) searchIndex = index;
      })
      .catch(() => {
        // The form remains the search fallback when the index is unavailable.
      });
    return () => {
      active = false;
    };
  });

  $effect(() => {
    query = data.q;
    suggestionsDismissed = false;
    activeSuggestion = -1;
  });

  // Any change to what the list contains puts the reader back on page one;
  // holding page 4 while the row count drops to six shows an empty column.
  $effect(() => {
    void data.entity;
    void data.q;
    void showVerified;
    void showUnverified;
    void showContradicted;
    notePage = 0;
  });

  function onSearchInput(event) {
    query = event.currentTarget.value;
    suggestionsDismissed = false;
    activeSuggestion = -1;
  }

  function onSearchKeydown(event) {
    if (event.key === "Escape") {
      suggestionsDismissed = true;
      activeSuggestion = -1;
      return;
    }
    if (!instantResults.length) return;
    if (event.key === "ArrowDown") {
      event.preventDefault();
      activeSuggestion = (activeSuggestion + 1) % instantResults.length;
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      activeSuggestion =
        activeSuggestion <= 0
          ? instantResults.length - 1
          : activeSuggestion - 1;
    } else if (event.key === "Enter" && activeSuggestion >= 0) {
      event.preventDefault();
      const note = instantResults[activeSuggestion];
      window.location.assign(
        `/app/notes?view=graph&focus=${encodeURIComponent(note.note_id)}`,
      );
    }
  }

  function markedTitle(title, query) {
    if (!query) return [{ text: title, match: false }];
    const lower = title.toLowerCase();
    const needle = query.toLowerCase();
    const out = [];
    let start = 0;
    let index = lower.indexOf(needle);
    while (index >= 0) {
      if (index > start)
        out.push({ text: title.slice(start, index), match: false });
      out.push({
        text: title.slice(index, index + needle.length),
        match: true,
      });
      start = index + needle.length;
      index = lower.indexOf(needle, start);
    }
    if (start < title.length)
      out.push({ text: title.slice(start), match: false });
    return out;
  }

  function projectNames(result) {
    return result.entities
      .filter((entity) => entity.kind === "project")
      .map((entity) => entity.title)
      .join(", ");
  }

  function projectCopy(project) {
    return PROJECT_COPY[project.slug === "tooling" ? "ci" : project.slug];
  }
</script>

<Seo
  title="Factory context · jomcgi.dev"
  description="The context engine behind the Ember Software Factory: a knowledge graph produced from agent observations."
  path="/slop/factory/context"
/>

<main class="td factory-page context-page">
  <div class="frame">
    <header class="masthead">
      <h1 class="sr-only">Ember Software Factory</h1>
      <Trail page="factory" />
      <div class="mast-actions">
        <nav class="view-tabs" aria-label="Factory views">
          <a href="/slop/factory">overview</a>
          <a class="here" href="/slop/factory/context" aria-current="page"
            >context</a
          >
        </nav>
        <SchemeToggle />
      </div>
    </header>

    {#if Object.values(data.unavailable).some(Boolean)}
      <p class="unavailable">Unavailable right now.</p>
    {/if}
    <div class="journal">
      <aside class="spine">
        <div>
          <p class="sec-label">/ Search</p>
          <form class="search" method="GET" action="/slop/factory/context">
            <input
              name="q"
              type="search"
              value={query}
              placeholder="Find a fact"
              aria-label="Search the record"
              autocomplete="off"
              aria-controls="factory-search-suggestions"
              aria-activedescendant={activeDescendant}
              aria-expanded={Boolean(
                searchIndex && query.trim() && !suggestionsDismissed,
              )}
              role="combobox"
              oninput={onSearchInput}
              onkeydown={onSearchKeydown}
            />
            <button type="submit">Find</button>
            {#if searchIndex && query.trim() && !suggestionsDismissed}
              <div class="suggestions">
                <p>
                  <span>Results</span><code>titles · instant</code>
                </p>
                <ul id="factory-search-suggestions" role="listbox">
                  {#each instantResults as result, index (result.note_id)}
                    <li
                      id={`factory-search-option-${index}`}
                      role="option"
                      aria-selected={activeSuggestion === index}
                    >
                      <a
                        class:active={activeSuggestion === index}
                        href={`/app/notes?view=graph&focus=${encodeURIComponent(result.note_id)}`}
                      >
                        <i class={`mark ${markClass(result)}`}></i>
                        <span>{result.title}</span>
                      </a>
                    </li>
                  {:else}
                    <li class="none" role="presentation">No title matches.</li>
                  {/each}
                </ul>
              </div>
            {/if}
          </form>
        </div>
        <div>
          <p class="sec-label">
            / Filter
            <button
              class="explain"
              type="button"
              aria-expanded={definitionsOpen}
              aria-controls="factory-mark-definitions"
              onclick={() => (definitionsOpen = !definitionsOpen)}
              onkeydown={(event) => {
                if (event.key === "Escape") definitionsOpen = false;
              }}>(?)<span class="sr-only">What the marks mean</span></button
            >
          </p>
          <div class="filter">
            <label
              ><input type="checkbox" bind:checked={showVerified} /><span
                ><i class="mark verified"></i>&nbsp; Verified</span
              ><span class="n num">{number(verifiedTotal)}</span></label
            >
            <label
              ><input type="checkbox" bind:checked={showUnverified} /><span
                ><i class="mark unverified"></i>&nbsp; Unverified</span
              ><span class="n num">{number(unverifiedTotal)}</span></label
            >
            <label
              ><input type="checkbox" bind:checked={showContradicted} /><span
                ><i class="mark disputed"></i>&nbsp; Contradicted</span
              ><span class="n num">{number(contradictedTotal)}</span></label
            >
          </div>
          {#if definitionsOpen}
            <dl id="factory-mark-definitions" class="definitions">
              {#each MARK_DEFINITIONS as mark}
                <div>
                  <dt><i class={`mark ${mark.state}`}></i>{mark.label}</dt>
                  <dd>{mark.definition}</dd>
                </div>
              {/each}
            </dl>
          {/if}
        </div>
        <div>
          <p class="sec-label">/ Topics</p>
          <nav class="topics" aria-label="Record topics">
            {#each data.projects as project, index}
              <a
                class:active={data.entity === project.slug && !data.q}
                class:group={index === 0}
                href={`/slop/factory/context?entity=${project.slug}`}
              >
                {project.title}<small
                  >{number(
                    count(project, "verified") + count(project, "unverified"),
                  )}</small
                >
              </a>
            {:else}
              {#if data.unavailable.entities}
                <span class="none">None recorded.</span>
              {/if}
            {/each}
          </nav>
        </div>
      </aside>

      <article class="doc">
        {#if data.q}
          <h2>
            Search <code class="search-kind">content + titles · Find</code>
          </h2>
          <p class="meta">
            <span>{visibleResults.length} matching titles or bodies</span>
          </p>
          <section>
            {#if data.unavailable.search}
              <p class="none">Nothing passes the current filter.</p>
            {:else}
              <ul class="results">
                {#each pagedRows.rows as result (result.note_id)}
                  <li>
                    <i class={`mark ${markClass(result)}`}></i>
                    <span
                      >{#each markedTitle(result.title, data.q) as part}{#if part.match}<mark
                            >{part.text}</mark
                          >{:else}{part.text}{/if}{/each}</span
                    >
                    <span class="proj">{projectNames(result)}</span>
                  </li>
                {:else}
                  <li>
                    <span></span><span class="none">No fact matches.</span>
                  </li>
                {/each}
              </ul>
              {#if pagedRows.pageCount > 1}
                <div class="pager">
                  <span
                    >{pagedRows.start}–{pagedRows.end} of {listRows.length}</span
                  >
                  <span
                    ><button
                      type="button"
                      onclick={() => (notePage -= 1)}
                      disabled={notePage === 0}>prev</button
                    ><button
                      type="button"
                      onclick={() => (notePage += 1)}
                      disabled={notePage >= pagedRows.pageCount - 1}
                      >next</button
                    ></span
                  >
                </div>
              {/if}
            {/if}
          </section>
        {:else if data.entity && (!data.chapter || !selected)}
          <h2>{selected?.title ?? data.entity}</h2>
          <p class="none">None recorded.</p>
        {:else if data.chapter && selected}
          <h2>{data.chapter.entity.title}</h2>
          <p class="lede">
            {projectCopy(selected)?.lede ??
              "The current public record for this project."}
          </p>
          <p class="meta">
            <span
              >{projectCopy(selected)?.scope ?? selected.scope ?? "repo"}</span
            ><span>last observed {day(lastObserved)}</span><span
              >{verified.length} verified</span
            ><span>{unverified.length} unverified</span><span
              >{data.chapter.contradictions.length} contradictions</span
            >
          </p>

          {#if showVerified}
            <section>
              <h3>
                <span>1</span><span>Current state</span>
              </h3>
              {#if verified.length}
                <ol>
                  {#each pagedRows.rows as fact (fact.note_id)}
                    <li>
                      <details>
                        <summary
                          ><i class="mark verified"></i><span>{fact.title}</span
                          ><time>{day(fact.observed_at)}</time></summary
                        >
                        <div class="body">
                          {fact.snippet}<span class="src"
                            >{fact.scope ?? "public record"} · confidence {fact.confidence ??
                              "·"}</span
                          >
                        </div>
                      </details>
                    </li>
                  {/each}
                </ol>
              {:else}<p class="none">Nothing verified yet.</p>{/if}
              {#if pagedRows.pageCount > 1}
                <div class="pager">
                  <span
                    >{pagedRows.start}–{pagedRows.end} of {listRows.length}</span
                  >
                  <span
                    ><button
                      type="button"
                      onclick={() => (notePage -= 1)}
                      disabled={notePage === 0}>prev</button
                    ><button
                      type="button"
                      onclick={() => (notePage += 1)}
                      disabled={notePage >= pagedRows.pageCount - 1}
                      >next</button
                    ></span
                  >
                </div>
              {/if}
            </section>
          {/if}

          {#if showUnverified}
            <section>
              <h3>
                <span>{showVerified ? 2 : 1}</span><span>Unverified</span>
              </h3>
              {#if unverified.length}
                <ol>
                  {#each unverified as fact (fact.note_id)}
                    <li>
                      <details>
                        <summary
                          ><i class="mark unverified"></i><span
                            >{fact.title}</span
                          ><time>{day(fact.observed_at)}</time></summary
                        >
                        <div class="body">
                          {fact.snippet}<span class="src"
                            >{fact.scope ?? "public record"} · confidence {fact.confidence ??
                              "·"}</span
                          >
                        </div>
                      </details>
                    </li>
                  {/each}
                </ol>
              {:else}<p class="none">Nothing unverified yet.</p>{/if}
            </section>
          {/if}

          {#if showContradicted}
            <section>
              <h3>
                <span>{Number(showVerified) + Number(showUnverified) + 1}</span
                ><span>Contradictions</span>
              </h3>
              {#each data.chapter.contradictions as contradiction, index}
                <div class="pair">
                  <span class="n">{String(index + 1).padStart(2, "0")}</span>
                  <div class="side">
                    <i class={`mark ${markClass(contradiction.a)}`}></i><span
                      >{contradiction.a.title}</span
                    >
                  </div>
                  <span class="vs">VS</span>
                  <div class="side">
                    <i class={`mark ${markClass(contradiction.b)}`}></i><span
                      >{contradiction.b.title}</span
                    >
                  </div>
                </div>
              {:else}<p class="none">None recorded.</p>{/each}
            </section>
          {/if}
          {#if noneShown}<p class="none">
              Nothing passes the current filter.
            </p>{/if}
        {:else}
          <h2>What is a context engine?</h2>
          <p class="lede">
            Context here is a knowledge graph produced from agent observations.
            <br />The engine maintains this by ingesting session transcripts,
            evaluating records against reality and investigating conflicting
            information.
          </p>
          <figure>
            <svg
              viewBox="0 44 780 208"
              role="img"
              aria-label="Four parts in a line, sessions to raw input to record to readers, with a dashed return path for disputes"
            >
              <g fill="none" stroke="currentColor" stroke-width="1.25">
                <rect x="20" y="56" width="148" height="130" /><rect
                  x="228"
                  y="56"
                  width="148"
                  height="130"
                /><rect x="436" y="56" width="148" height="130" /><rect
                  x="644"
                  y="56"
                  width="116"
                  height="130"
                />
              </g>
              <g stroke="currentColor" stroke-width="1">
                <line x1="20" y1="80" x2="168" y2="80" /><line
                  x1="228"
                  y1="80"
                  x2="376"
                  y2="80"
                /><line x1="436" y1="80" x2="584" y2="80" /><line
                  x1="644"
                  y1="80"
                  x2="760"
                  y2="80"
                />
              </g>
              <g font-size="11" fill="currentColor">
                <text x="28" y="72">sessions</text><text x="28" y="102"
                  >codex</text
                ><text x="28" y="120">claude</text><text x="28" y="138"
                  >ember</text
                ><text x="28" y="156">agent report</text>
                <text x="236" y="72">raw input</text><text x="236" y="102"
                  >immutable</text
                ><text x="236" y="120">content hash</text><text x="236" y="138"
                  >source lane</text
                >
                <text x="444" y="72">record</text><text x="444" y="102"
                  >verified</text
                ><text x="444" y="120">unverified</text><text x="444" y="138"
                  >scope</text
                ><text x="444" y="156">confidence</text><text x="444" y="174"
                  >validity window</text
                >
                <text x="652" y="72">readers</text><text x="652" y="102"
                  >agents</text
                ><text x="652" y="120">this page</text>
              </g>
              <g stroke="currentColor" stroke-width="1" fill="currentColor">
                <line x1="170" y1="121" x2="220" y2="121" /><polygon
                  points="226,121 219,117.5 219,124.5"
                /><line x1="378" y1="121" x2="428" y2="121" /><polygon
                  points="434,121 427,117.5 427,124.5"
                /><line x1="586" y1="121" x2="636" y2="121" /><polygon
                  points="642,121 635,117.5 635,124.5"
                />
              </g>
              <path
                d="M702 186 V218 H302 V188"
                fill="none"
                stroke="currentColor"
                stroke-width="1"
                stroke-dasharray="3 3"
              /><polygon
                points="302,186 298.5,193 305.5,193"
                fill="currentColor"
              /><text
                x="502"
                y="238"
                font-size="11"
                fill="currentColor"
                text-anchor="middle"
                >dispute: a new raw input, the fact stays</text
              >
            </svg>
            <figcaption>Fig. 1 Context data flow</figcaption>
          </figure>
          <section>
            <h3><span>1</span><span>What the marks mean</span></h3>
            <ol>
              {#each MARK_DEFINITIONS as mark}
                <li>
                  <details>
                    <summary
                      ><i class={`mark ${mark.state}`}></i><span
                        >{mark.label}: {mark.definition}.</span
                      ><time>{number(markTotals[mark.state])}</time></summary
                    >
                  </details>
                </li>
              {/each}
            </ol>
          </section>
        {/if}
      </article>
    </div>
  </div>
</main>
