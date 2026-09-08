<script>
  import { Seo } from "$lib/public/components";
  import "$lib/public/factory/factory.css";
  import { markClass } from "$lib/public/factory/model.js";
  import Trail from "../../Trail.svelte";

  let { data } = $props();
  let showVerified = $state(true);
  let showUnverified = $state(true);

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
  const selected = $derived(
    data.projects.find((project) => project.slug === data.entity),
  );
  const verified = $derived(
    (data.chapter?.notes ?? []).filter(
      (note) => note.verification_state === "verified",
    ),
  );
  const unverified = $derived(
    (data.chapter?.notes ?? []).filter(
      (note) => note.verification_state === "unverified",
    ),
  );
  const visibleResults = $derived(
    data.results.filter((result) =>
      result.verification_state === "verified"
        ? showVerified
        : result.verification_state === "unverified"
          ? showUnverified
          : false,
    ),
  );
  const lastObserved = $derived(
    (data.chapter?.notes ?? [])
      .map((note) => note.observed_at)
      .filter(Boolean)
      .sort()
      .at(-1),
  );

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
  title="Factory record · jomcgi.dev"
  description="The published record behind the Ember Software Factory."
  path="/slop/factory/record"
/>

<main class="td factory-page record-page">
  <div class="frame">
    <header class="masthead">
      <div>
        <h1>Ember Software Factory</h1>
        <p>What the agents merged, what it cost, what they learned.</p>
      </div>
      <div class="mast-trails">
        <Trail page="factory" />
        <nav class="view-tabs" aria-label="Factory views">
          <a href="/slop/factory">overview</a>
          <a class="here" href="/slop/factory/record" aria-current="page"
            >record</a
          >
        </nav>
      </div>
    </header>

    {#if Object.values(data.unavailable).some(Boolean)}
      <p class="unavailable">Unavailable right now.</p>
    {/if}
    <div class="journal">
      <aside class="spine">
        <div>
          <p class="sec-label">/ Search</p>
          <form class="search" method="GET" action="/slop/factory/record">
            <input
              name="q"
              type="search"
              value={data.q}
              placeholder="Find a fact"
              aria-label="Search the record"
            />
            <button type="submit">Find</button>
            <input name="mode" type="hidden" value={data.mode} />
            <div class="modes">
              <a
                class:on={data.mode === "grep"}
                href={`/slop/factory/record?q=${encodeURIComponent(data.q)}&mode=grep`}
                >grep</a
              >
              <a
                class:on={data.mode === "semantic"}
                href={`/slop/factory/record?q=${encodeURIComponent(data.q)}&mode=semantic`}
                >semantic</a
              >
            </div>
          </form>
        </div>
        <div>
          <p class="sec-label">/ Show</p>
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
          </div>
        </div>
        <div>
          <p class="sec-label">/ Index</p>
          <nav class="index" aria-label="Record index">
            <a
              class:active={!data.entity && !data.q}
              href="/slop/factory/record"
              >How the record is kept<small></small></a
            >
            {#each data.projects as project, index}
              <a
                class:active={data.entity === project.slug && !data.q}
                class:group={index === 0}
                href={`/slop/factory/record?entity=${project.slug}`}
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
          <h2>Search</h2>
          <p class="meta">
            <span
              >{data.mode === "grep"
                ? `grep · ${visibleResults.length} matching titles or bodies`
                : "semantic · nearest by embedding"}</span
            >
          </p>
          <section>
            {#if data.unavailable.search}
              <p class="none">Nothing passes the current filter.</p>
            {:else}
              <ul class="results">
                {#each visibleResults as result (result.note_id)}
                  <li>
                    <i class={`mark ${markClass(result)}`}></i>
                    <span
                      >{#each markedTitle(result.title, data.mode === "grep" ? data.q : "") as part}{#if part.match}<mark
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
                <span>1</span><span
                  >Current state<small>verified, newest first</small></span
                >
              </h3>
              {#if verified.length}
                <ol>
                  {#each verified as fact (fact.note_id)}
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
            </section>
          {/if}

          {#if showUnverified}
            <section>
              <h3>
                <span>{showVerified ? 2 : 1}</span><span
                  >Unconfirmed<small
                    >extracted, awaiting a second observation</small
                  ></span
                >
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
              {:else}<p class="none">Nothing unconfirmed.</p>{/if}
            </section>
          {/if}

          <section>
            <h3>
              <span>{Number(showVerified) + Number(showUnverified) + 1}</span
              ><span
                >Contradictions<small
                  >both facts stay until a dispute settles it</small
                ></span
              >
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
          {#if !showVerified && !showUnverified}<p class="none">
              Nothing passes the current filter.
            </p>{/if}
        {:else}
          <h2>How the record is kept</h2>
          <p class="lede">
            Agents do not write to this page. They produce evidence; a separate
            job decides what, if anything, becomes a fact.
          </p>
          <figure>
            <svg
              viewBox="0 0 780 252"
              role="img"
              aria-label="Exploded view: five parts in a line, sessions to raw input to drain to record to readers, with a dashed return path for disputes"
            >
              <g fill="none" stroke="currentColor" stroke-width="1.25">
                <rect x="20" y="56" width="124" height="130" /><rect
                  x="184"
                  y="56"
                  width="124"
                  height="130"
                /><rect x="348" y="56" width="124" height="130" /><rect
                  x="512"
                  y="56"
                  width="124"
                  height="130"
                /><rect x="676" y="56" width="84" height="130" />
              </g>
              <g stroke="currentColor" stroke-width="1">
                <line x1="20" y1="80" x2="144" y2="80" /><line
                  x1="184"
                  y1="80"
                  x2="308"
                  y2="80"
                /><line x1="348" y1="80" x2="472" y2="80" /><line
                  x1="512"
                  y1="80"
                  x2="636"
                  y2="80"
                /><line x1="676" y1="80" x2="760" y2="80" />
              </g>
              <g font-size="11" fill="currentColor">
                <text x="28" y="72">sessions</text><text x="28" y="102"
                  >codex</text
                ><text x="28" y="120">claude</text><text x="28" y="138"
                  >ember</text
                ><text x="28" y="156">agent report</text>
                <text x="192" y="72">raw input</text><text x="192" y="102"
                  >immutable</text
                ><text x="192" y="120">content hash</text><text x="192" y="138"
                  >source lane</text
                >
                <text x="356" y="72">drain</text><text x="356" y="102"
                  >lens, extract</text
                ><text x="356" y="120">drop run noise</text><text
                  x="356"
                  y="138">drop bare values</text
                ><text x="356" y="156">drop duplicates</text>
                <text x="520" y="72">record</text><text x="520" y="102"
                  >■ verified</text
                ><text x="520" y="120">□ unverified</text><text x="520" y="138"
                  >scope</text
                ><text x="520" y="156">confidence</text><text x="520" y="174"
                  >validity window</text
                >
                <text x="684" y="72">readers</text><text x="684" y="102"
                  >agents</text
                ><text x="684" y="120">this page</text>
              </g>
              <g stroke="currentColor" stroke-width="1" fill="currentColor">
                <line x1="146" y1="121" x2="176" y2="121" /><polygon
                  points="182,121 175,117.5 175,124.5"
                /><line x1="310" y1="121" x2="340" y2="121" /><polygon
                  points="346,121 339,117.5 339,124.5"
                /><line x1="474" y1="121" x2="504" y2="121" /><polygon
                  points="510,121 503,117.5 503,124.5"
                /><line x1="638" y1="121" x2="668" y2="121" /><polygon
                  points="674,121 667,117.5 667,124.5"
                />
              </g>
              <g fill="none" stroke="currentColor" stroke-width="1">
                <circle cx="82" cy="26" r="9" /><line
                  x1="82"
                  y1="35"
                  x2="82"
                  y2="54"
                /><circle cx="246" cy="26" r="9" /><line
                  x1="246"
                  y1="35"
                  x2="246"
                  y2="54"
                /><circle cx="410" cy="26" r="9" /><line
                  x1="410"
                  y1="35"
                  x2="410"
                  y2="54"
                /><circle cx="574" cy="26" r="9" /><line
                  x1="574"
                  y1="35"
                  x2="574"
                  y2="54"
                /><circle cx="718" cy="26" r="9" /><line
                  x1="718"
                  y1="35"
                  x2="718"
                  y2="54"
                />
              </g>
              <g fill="currentColor"
                ><circle cx="82" cy="54" r="2" /><circle
                  cx="246"
                  cy="54"
                  r="2"
                /><circle cx="410" cy="54" r="2" /><circle
                  cx="574"
                  cy="54"
                  r="2"
                /><circle cx="718" cy="54" r="2" /></g
              >
              <g font-size="11" fill="currentColor" text-anchor="middle"
                ><text x="82" y="30">1</text><text x="246" y="30">2</text><text
                  x="410"
                  y="30">3</text
                ><text x="574" y="30">4</text><text x="718" y="30">5</text></g
              >
              <path
                d="M718 186 V218 H246 V188"
                fill="none"
                stroke="currentColor"
                stroke-width="1"
                stroke-dasharray="3 3"
              /><polygon
                points="246,186 242.5,193 249.5,193"
                fill="currentColor"
              /><text
                x="482"
                y="238"
                font-size="11"
                fill="currentColor"
                text-anchor="middle"
                >dispute: a new raw input, the fact stays</text
              >
            </svg>
            <figcaption>
              Fig. 1. A session becomes an immutable raw input; the drain
              extracts and gates; the record keeps what survives; agents and
              this page read the same rows. A dispute is a new raw input and
              never deletes.
            </figcaption>
            <div class="key">
              <div>
                <span>1</span><span
                  >Sessions<small>one raw per session</small></span
                >
              </div>
              <div>
                <span>2</span><span
                  >Raw input<small>immutable, content-addressed</small></span
                >
              </div>
              <div>
                <span>3</span><span
                  >Drain<small>drops run noise and duplicates</small></span
                >
              </div>
              <div>
                <span>4</span><span
                  >Record<small>what this page shows</small></span
                >
              </div>
              <div>
                <span>5</span><span>Readers<small>agents, then you</small></span
                >
              </div>
            </div>
          </figure>
          <section>
            <h3><span>1</span><span>What the marks mean</span></h3>
            <ol>
              <li>
                <details>
                  <summary
                    ><i class="mark verified"></i><span
                      >Verified: the fact points at a file, path, or commit that
                      exists on main.</span
                    ><time>{number(verifiedTotal)}</time></summary
                  >
                </details>
              </li>
              <li>
                <details>
                  <summary
                    ><i class="mark unverified"></i><span
                      >Unverified: extracted, not yet grounded.</span
                    ><time>{number(unverifiedTotal)}</time></summary
                  >
                </details>
              </li>
              <li>
                <details>
                  <summary
                    ><i class="mark disputed"></i><span
                      >Contradiction: a newer fact that cannot hold alongside an
                      older one. Both stay.</span
                    ><time>{number(data.facts.contradictions)}</time></summary
                  >
                </details>
              </li>
            </ol>
          </section>
        {/if}
      </article>
    </div>
  </div>
</main>
