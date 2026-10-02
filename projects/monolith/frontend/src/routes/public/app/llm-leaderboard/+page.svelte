<script>
  import { replaceState } from "$app/navigation";
  import { page } from "$app/state";
  import { SchemeToggle, Seo } from "$lib/public/components";
  import BarChart from "$lib/public/llm-leaderboard/BarChart.svelte";
  import Scatter from "$lib/public/llm-leaderboard/Scatter.svelte";
  import Selector from "$lib/public/llm-leaderboard/Selector.svelte";
  import TaskMatrix from "$lib/public/llm-leaderboard/TaskMatrix.svelte";
  import "$lib/public/llm-leaderboard/leaderboard.css";
  import {
    METRICS,
    fmtDate,
    hardRate,
    kfmt,
    money,
    orderedTasks,
    parseSelection,
    providerSlot,
    rank,
    secs,
    serializeSelection,
    shortName,
  } from "$lib/public/llm-leaderboard/model.js";
  import "$lib/public/styles/technical-drawing.css";

  let { data } = $props();

  const lb = $derived(data.leaderboard ?? {});
  const models = $derived(lb.models ?? []);
  const tasks = $derived(lb.tasks ?? []);
  const keyed = $derived(orderedTasks(tasks));
  const ranked = $derived(rank(models));

  // The selection is the page's one piece of state. It starts from ?m= so a
  // view can be linked, and writes back with a shallow replaceState so
  // picking models never re-runs the load or adds history entries.
  // The snapshot is static per deploy, so reading `data` once here is right.
  // svelte-ignore state_referenced_locally
  let selected = $state(
    parseSelection(data.leaderboard?.models ?? [], page.url.searchParams.get("m")),
  );
  let hot = $state(null);
  const shown = $derived(ranked.filter((m) => selected.has(m.id)));

  function choose(next) {
    selected = next;
    const url = new URL(page.url);
    const value = serializeSelection(models, next);
    if (value) url.searchParams.set("m", value);
    else url.searchParams.delete("m");
    replaceState(url, {});
  }

  // Table sort. Rank is the default; any column header re-sorts.
  let sortKey = $state("rank");
  let sortDir = $state(1);
  const COLS = [
    ["rank", "#"],
    ["name", "model"],
    ["hard", "hard"],
    ["floor", "floor"],
    ["cost", "$ / task"],
    ["solve", "$ / solve"],
    ["wall", "wall / task"],
    ["tokens", "tokens / task"],
    ["turns", "steps"],
    ["tools", "tool calls ok"],
  ];
  const SORT_GET = {
    rank: (m) => ranked.indexOf(m),
    name: (m) => shortName(m),
    hard: (m) => -hardRate(m),
    floor: (m) => -(m.floor_n ? m.floor_pass / m.floor_n : 0),
    cost: (m) => m.cost_usd ?? 0,
    solve: (m) => m.cost_per_solve_usd ?? Infinity,
    wall: (m) => m.mean_latency_ms ?? 0,
    tokens: (m) => m.mean_tokens ?? 0,
    turns: (m) => m.mean_turns ?? 0,
    tools: (m) => -(m.tool_use_ok ?? 0),
  };
  const rows = $derived.by(() => {
    const get = SORT_GET[sortKey];
    return [...shown].sort((a, b) => {
      const x = get(a);
      const y = get(b);
      return sortDir * (typeof x === "string" ? x.localeCompare(y) : x - y);
    });
  });
  function sortBy(key) {
    if (sortKey === key) sortDir *= -1;
    else {
      sortKey = key;
      sortDir = 1;
    }
  }

  const leader = $derived(ranked[0]);
  const cheapestPerfect = $derived(
    [...models]
      .filter(
        (m) =>
          hardRate(m) >= 1 &&
          m.role !== "anchor" &&
          !m.self_hosted &&
          m.cost_per_solve_usd > 0,
      )
      .sort((a, b) => a.cost_per_solve_usd - b.cost_per_solve_usd)[0],
  );
  const qualified = $derived(models.filter((m) => m.qualified).length);
  const hardCount = $derived(tasks.filter((t) => t.tier === "hard").length);
  const realCount = $derived(tasks.filter((t) => t.real_test).length);
</script>

<Seo
  title="LLM Leaderboard · jomcgi.dev"
  description="Agentic coding benchmark over a real homelab monolith: which budget and self-hosted LLMs can do the work, graded by the repo's own tests."
  path="/app/llm-leaderboard"
/>

<main class="td lb-page">
  <div class="frame">
    <header class="masthead">
      <nav class="trail" aria-label="You are here">
        <a class="crumb" href="/">jomcgi.dev</a>
        <span class="crumb current" aria-current="page">llm-leaderboard</span>
      </nav>
      <SchemeToggle />
    </header>

    <div class="lead">
      <h1>Which models can do the homelab's real work?</h1>
      <p>
        Each model gets a snapshot of this repo taken just before a real fix,
        file tools, and the issue. It makes the change itself over several
        turns, then the fix commit's own test grades it. Easy and standard tasks
        are the floor a model must clear; the hard tasks, cost and speed rank
        the ones that do. Claude is the ceiling, not a competitor.
      </p>
    </div>

    <div class="stats">
      <div>
        <div class="k">Leader</div>
        <div class="v">{leader ? `${leader.hard_pass}/${leader.hard_n}` : "n/a"}<small>hard</small></div>
        <div class="m">{leader ? shortName(leader) : ""}</div>
      </div>
      <div>
        <div class="k">Cheapest full marks</div>
        <div class="v">
          {cheapestPerfect ? money(cheapestPerfect.cost_per_solve_usd) : "n/a"}<small>/ solve</small>
        </div>
        <div class="m">
          {cheapestPerfect ? `${shortName(cheapestPerfect)} · ${cheapestPerfect.hard_pass}/${cheapestPerfect.hard_n} hard` : ""}
        </div>
      </div>
      <div>
        <div class="k">Models</div>
        <div class="v">{models.length}</div>
        <div class="m">{qualified} cleared the floor</div>
      </div>
      <div>
        <div class="k">Tasks</div>
        <div class="v">{tasks.length}</div>
        <div class="m">{hardCount} hard · {realCount} repo-tested</div>
      </div>
      <div>
        <div class="k">Updated</div>
        <div class="v">{fmtDate(lb.generated_at)}</div>
        <div class="m">harness {lb.harness_version}</div>
      </div>
    </div>

    <Selector {models} {selected} onchange={choose} />

    <section>
      <p class="sec-label">/ Headline metrics</p>
      <div class="grid2">
        {#each ["hard", "cost", "wall", "tokens"] as metric}
          <BarChart models={shown} {metric} {hot} onhover={(id) => (hot = id)} />
        {/each}
      </div>
      <p class="caption">
        Means per task, so one task blowing up stays visible. Cost is list
        price through OpenRouter; Claude rows use the representative API price
        though they ran through Claude Code. Self-hosted rows cost $0 and their
        wall-time is a single RTX 4090, so compare them on pass and tokens.
      </p>
    </section>

    <section>
      <p class="sec-label">/ Capability vs efficiency</p>
      <Scatter models={shown} {hot} onhover={(id) => (hot = id)} />
    </section>

    <section>
      <p class="sec-label">/ Every task</p>
      <TaskMatrix models={shown} {tasks} {hot} onhover={(id) => (hot = id)} />
    </section>

    <section>
      <p class="sec-label">
        / Ranked
        <span class="aside">hard-task pass rate, then $ per solve</span>
      </p>
      <div class="panel table-wrap">
        <table class="ranked" class:hovering={hot}>
          <thead>
            <tr>
              {#each COLS as [key, label], i}
                <th class:l={i < 2} aria-sort={sortKey === key ? (sortDir > 0 ? "ascending" : "descending") : undefined}>
                  <button type="button" class:on={sortKey === key} onclick={() => sortBy(key)}
                    >{label}{sortKey === key ? (sortDir > 0 ? " ↓" : " ↑") : ""}</button
                  >
                </th>
              {/each}
            </tr>
          </thead>
          <tbody>
            {#each rows as m (m.id)}
              <tr
                data-model={m.id}
                class:hot={hot === m.id}
                onmouseenter={() => (hot = m.id)}
                onmouseleave={() => (hot = null)}
              >
                <td class="l num rk">{ranked.indexOf(m) + 1}</td>
                <td class="l">
                  <span class="nm"><i class="sw" data-slot={providerSlot(m.id)}></i>{shortName(m)}</span>
                  <span class="slug">{m.id}{#if m.role === "anchor"} · ceiling{:else if m.self_hosted} · self-hosted{/if}</span>
                </td>
                <td class="num">{m.hard_pass}/{m.hard_n}</td>
                <td class="num">{m.floor_pass}/{m.floor_n}</td>
                <td class="num">{money(m.cost_usd)}</td>
                <td class="num">{money(m.cost_per_solve_usd)}</td>
                <td class="num">{secs((m.mean_latency_ms ?? 0) / 1000)}</td>
                <td class="num">{m.role === "anchor" ? "n/a" : kfmt(m.mean_tokens)}</td>
                <td class="num">{m.role === "anchor" ? "n/a" : METRICS.turns.fmt(m.mean_turns)}</td>
                <td class="num">{Math.round((m.tool_use_ok ?? 0) * 100)}%</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
      <p class="caption">
        Hard and floor count graded tasks; cells that errored before grading
        (a provider fault or a prompt past the context window) are left out
        rather than scored as failures. Claude's steps and tokens come from
        its own harness and are not comparable to the candidate rows.
      </p>
    </section>

    <section>
      <p class="sec-label">
        / Tasks
        <span class="aside">passed / ran, across all {models.length} models</span>
      </p>
      <ol class="panel tasks">
        {#each keyed as t (t.id)}
          <li id={`task-${t.no}`}>
            <span class="n num">{t.no}</span>
            <span class="body">
              <span class="id">{t.id}</span>
              <span class="blurb">{t.blurb}</span>
            </span>
            <span class="meta">{t.tier} · {t.real_test ? "repo test" : "behavioural"}</span>
            <span class="score num">{t.passed}/{t.n}</span>
          </li>
        {/each}
      </ol>
    </section>

    <footer class="caption method">
      Method: SWE-bench style. Each task snapshots the parent of a real fix
      commit, the model edits it through list, read and write tools, and the
      fix commit's gold test runs against the result. A "repo test" task is
      graded by the monolith's own pytest suite, a "behavioural" one by a
      hand-written check. Source:
      <a href="https://github.com/jomcgi/homelab/tree/main/projects/model-bench"
        >projects/model-bench</a
      >.
    </footer>
  </div>
</main>

<style>
  .table-wrap {
    overflow-x: auto;
  }

  .ranked {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.82rem;
  }

  .ranked th {
    padding: 0.45em 0.7em;
    border-bottom: 1px solid var(--stroke);
    background: var(--band);
    font-weight: 500;
    text-align: right;
    white-space: nowrap;
  }

  .ranked th button {
    padding: 0 0 2px;
    border: 0;
    border-bottom: 2px solid transparent;
    background: none;
    color: var(--ink-2);
    cursor: pointer;
    font-family: var(--font-code);
    font-size: 0.66rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .ranked th button:hover {
    color: var(--accent-ink);
  }

  .ranked th button.on {
    border-bottom-color: var(--accent-ink);
    color: var(--ink);
  }

  .ranked td {
    padding: 0.4em 0.7em;
    font-family: var(--font-code);
    font-size: 0.74rem;
    text-align: right;
    white-space: nowrap;
  }

  .ranked tbody tr + tr td {
    border-top: 1px solid var(--line);
  }

  .ranked .l {
    text-align: left;
  }

  .ranked .rk {
    width: 2.5em;
    color: var(--ink-2);
  }

  .nm {
    display: flex;
    gap: 0.45em;
    align-items: center;
    color: var(--ink);
    font-family: var(--font-ui);
    font-size: 0.86rem;
    font-weight: 600;
  }

  .slug {
    color: var(--ink-2);
    font-size: 0.66rem;
  }

  .tasks {
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .tasks li {
    display: grid;
    grid-template-columns: 2.4em minmax(0, 1fr) auto 3.5em;
    gap: 0.8em;
    align-items: baseline;
    padding: 0.45em 0.8em;
    font-size: 0.82rem;
  }

  .tasks li + li {
    border-top: 1px solid var(--line);
  }

  .tasks li:target {
    background: var(--band);
    box-shadow: inset 2px 0 0 var(--accent-ink);
  }

  .tasks .n {
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.72rem;
  }

  .tasks .body {
    display: flex;
    flex-direction: column;
    min-width: 0;
  }

  .tasks .id {
    font-family: var(--font-code);
    font-size: 0.74rem;
    font-weight: 600;
  }

  .tasks .blurb {
    color: var(--ink-2);
  }

  .tasks .meta,
  .tasks .score {
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.7rem;
    text-align: right;
    white-space: nowrap;
  }

  .method {
    max-width: 48em;
  }

  @media (max-width: 640px) {
    .tasks li {
      grid-template-columns: 2em minmax(0, 1fr) 3em;
    }

    .tasks .meta {
      display: none;
    }
  }
</style>
