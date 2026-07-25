<script>
  import Scatter from "$lib/public/llm-leaderboard/Scatter.svelte";

  let { data } = $props();

  const lb = $derived(data.leaderboard ?? {});
  const models = $derived(lb.models ?? []);
  const tasks = $derived(lb.tasks ?? []);
  const realTestCount = $derived(tasks.filter((t) => t.real_test).length);
  // Join per-model task cells back to task metadata (real-test flag, blurb) by id.
  const taskById = $derived(Object.fromEntries(tasks.map((t) => [t.id, t])));
  // Gate model: qualified models cleared the easy+standard floor; the rest are out.
  const qualified = $derived(models.filter((m) => m.qualified));
  const disqualified = $derived(models.filter((m) => !m.qualified));
  const floorCount = $derived(tasks.filter((t) => t.tier !== "hard").length);
  const hardCount = $derived(tasks.filter((t) => t.tier === "hard").length);

  const num = (x) => (x ?? 0).toLocaleString("en-US");
  const money = (x) => `$${(x ?? 0).toFixed(4)}`;
  const secs = (ms) => (ms == null ? "n/a" : `${(ms / 1000).toFixed(1)}s`);
  // Drop the provider prefix for the headline name, keep the full slug beneath.
  const shortName = (id) =>
    id.includes("/") ? id.split("/").slice(1).join("/") : id;
  const isAnchor = (m) => m.role === "anchor";

  // Deep-dive: a row expands into its per-task breakdown. Guard on the data so the
  // page still renders if a leaderboard.json predates the per-task field (the rows
  // just stay non-expandable until it is regenerated).
  const hasBreakdown = (m) => Array.isArray(m.tasks) && m.tasks.length > 0;
  const solvedCount = (m) => (m.tasks ?? []).filter((t) => t.passed).length;
  let openId = $state(null); // accordion: at most one model open at a time
  const toggle = (m) => {
    if (!hasBreakdown(m)) return;
    openId = openId === m.id ? null : m.id;
  };
  const onRowKey = (e, m) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      toggle(m);
    }
  };

  const toolLabel = (t) => (t >= 0.999 ? "ok" : t > 0 ? "flaky" : "none");
</script>

<svelte:head>
  <title>LLM Leaderboard · jomcgi.dev</title>
  <meta
    name="description"
    content="Agentic coding benchmark: which budget LLMs can actually do offloadable homelab work, ranked by reliability, token efficiency, and cost, graded by the repo's own tests."
  />
</svelte:head>

<div class="page">
  <header class="hero">
    <div class="hero-mark">MODEL-BENCH</div>
    <h1>LLM Leaderboard</h1>
    <p class="lede">
      An <strong>agentic</strong> coding benchmark over this homelab's real
      monolith. Each model is dropped into a snapshot of the repo with file
      tools and has to make the change itself over multiple turns; it is then
      graded by the repo's
      <strong>own tests</strong>. Tasks are tiered: a model must clear every
      <strong>easy + standard</strong> task to <strong>qualify</strong> as
      viable, and the <strong>hard</strong> tasks plus cost and speed rank the ones
      that do.
    </p>
    <div class="meta">
      <span>{qualified.length}/{models.length} qualified</span>
      <span class="dot">·</span>
      <span>{floorCount} floor + {hardCount} hard tasks</span>
      <span class="dot">·</span>
      <span>{realTestCount} real-test</span>
      <span class="dot">·</span>
      <span>harness {lb.harness_version}</span>
      <span class="dot">·</span>
      <span>{lb.generated_at}</span>
    </div>
  </header>

  <section class="panel">
    <div class="panel-head">Pass rate vs efficiency</div>
    <Scatter {models} {tasks} />
  </section>

  <section class="panel">
    <div class="panel-head">
      Qualified: ranked by hard tasks, then cost
      <span class="panel-hint">click a row for its per-task breakdown</span>
    </div>
    <div class="scroll">
      <table>
        <thead>
          <tr>
            <th class="rk">#</th>
            <th class="mdl">Model</th>
            <th class="n">Hard</th>
            <th class="n">Tokens</th>
            <th class="n">Turns</th>
            <th class="n">Wall-time</th>
            <th class="n">Cost</th>
            <th class="n">$/solve</th>
            <th class="n">Tools</th>
          </tr>
        </thead>
        <tbody>
          {#each qualified as m, i}
            <!-- svelte-ignore a11y_no_static_element_interactions -->
            <tr
              class:winner={i === 0}
              class:anchor-row={isAnchor(m)}
              class:expandable={hasBreakdown(m)}
              class:open={openId === m.id}
              role={hasBreakdown(m) ? "button" : undefined}
              tabindex={hasBreakdown(m) ? 0 : undefined}
              aria-expanded={hasBreakdown(m) ? openId === m.id : undefined}
              aria-controls={hasBreakdown(m) ? `bd-detail-${i}` : undefined}
              onclick={() => toggle(m)}
              onkeydown={(e) => onRowKey(e, m)}
            >
              <td class="rk">
                {#if hasBreakdown(m)}<span class="chev" aria-hidden="true"
                    >{openId === m.id ? "▾" : "▸"}</span
                  >{/if}{i + 1}
              </td>
              <td class="mdl">
                <span class="name">{m.name ?? shortName(m.id)}</span>
                <span class="slug">{m.id}</span>
                {#if m.role === "anchor"}<span class="tag anchor">anchor</span
                  >{/if}
              </td>
              <td class="n">
                <span
                  class="rate {m.hard_pass === m.hard_n ? 'full' : 'partial'}"
                  >{m.hard_pass}/{m.hard_n}</span
                >
              </td>
              <td class="n mono">{num(m.mean_tokens)}</td>
              <td class="n mono">{m.mean_turns}</td>
              <td class="n mono">{secs(m.mean_latency_ms)}</td>
              <td class="n mono">{money(m.cost_usd)}</td>
              <td class="n mono"
                >{m.cost_per_solve_usd == null
                  ? "n/a"
                  : money(m.cost_per_solve_usd)}</td
              >
              <td class="n">
                <span class="pill {toolLabel(m.tool_use_ok)}"
                  >{toolLabel(m.tool_use_ok)}</span
                >
              </td>
            </tr>
            {#if openId === m.id && hasBreakdown(m)}
              <tr class="detail-row" id={`bd-detail-${i}`}>
                <td colspan="9">
                  <div class="detail">
                    <div class="detail-head">
                      Per-task breakdown
                      <span class="detail-sub"
                        >solved {solvedCount(m)}/{m.tasks.length}</span
                      >
                    </div>
                    <ul class="bd">
                      {#each m.tasks as bt (bt.id)}
                        {@const meta = taskById[bt.id]}
                        <li>
                          <span class="bd-status {bt.passed ? 'pass' : 'fail'}"
                            >{bt.passed ? "PASS" : "FAIL"}</span
                          >
                          <span class="bd-main">
                            <span class="bd-id">{bt.id}</span>
                            {#if meta?.real_test}
                              <span class="tag real">repo test</span>
                            {:else if meta}
                              <span class="tag synth">behavioural</span>
                            {/if}
                          </span>
                          <span class="bd-nums mono">
                            <span>{num(bt.tokens)} tok</span>
                            <span>{bt.turns} turns</span>
                            <span>{secs(bt.latency_ms)}</span>
                          </span>
                        </li>
                      {/each}
                    </ul>
                  </div>
                </td>
              </tr>
            {/if}
          {/each}
        </tbody>
      </table>
    </div>
    <div class="legend">
      <span
        ><b>Tokens / Turns / Wall-time</b> are the <b>mean per task</b> (not the median):
        the tasks vary ~5x in size, so the mean keeps a blow-up on one hard task visible
        instead of hiding it. Open a row for the per-task split.</span
      >
      <span
        ><b>Hard / Tokens / Turns / Tools</b> are model-intrinsic (the
        <b>self-host lens</b>); <b>Wall-time / Cost / $-per-solve</b> are the
        <b>cloud lens</b>: the real time and money to rent this model versus the
        Claude
        <b>anchor</b> rows you would be replacing. Wall-time is via OpenRouter.</span
      >
    </div>
  </section>

  {#if disqualified.length}
    <section class="panel">
      <div class="panel-head">Disqualified: missed a floor task</div>
      <div class="scroll">
        <table>
          <thead>
            <tr>
              <th class="mdl">Model</th>
              <th class="n">Floor</th>
              <th class="fail">Failed floor tasks</th>
              <th class="n">Tools</th>
            </tr>
          </thead>
          <tbody>
            {#each disqualified as m}
              <tr class="dq">
                <td class="mdl">
                  <span class="name">{m.name ?? shortName(m.id)}</span>
                  <span class="slug">{m.id}</span>
                </td>
                <td class="n"
                  ><span class="rate zero">{m.floor_pass}/{m.floor_n}</span></td
                >
                <td class="fail"
                  >{(m.floor_failed ?? []).join(", ") || "(none run)"}</td
                >
                <td class="n">
                  <span class="pill {toolLabel(m.tool_use_ok)}"
                    >{toolLabel(m.tool_use_ok)}</span
                  >
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    </section>
  {/if}

  <section class="panel">
    <div class="panel-head">The tasks</div>
    <ul class="tasks">
      {#each tasks as t}
        <li>
          <div class="t-top">
            <span class="t-id">{t.id}</span>
            <span class="tag tier-{t.tier}">{t.tier}</span>
            {#if t.real_test}
              <span class="tag real">repo test</span>
            {:else}
              <span class="tag synth">behavioural</span>
            {/if}
            <span class="t-score mono">{t.passed}/{t.n}</span>
          </div>
          <p class="t-blurb">{t.blurb}</p>
        </li>
      {/each}
    </ul>
  </section>

  <footer class="note">
    <p>
      SWE-bench style: each task snapshots the <em>parent</em> of a real fix
      commit (the buggy state), the model edits the snapshot through file tools,
      and the fix commit's gold test is run against the result on a vendored
      venv. A "repo test" task is graded by the monolith's own pytest suite; a
      "behavioural" one by a hand-written check. Model calls run through
      OpenRouter at list price. Tasks carry a difficulty <em>tier</em>: easy +
      standard form the qualification floor (miss one and a model is
      disqualified as not yet viable), while the hard tasks and the cost and
      speed columns rank the ones that clear it. Regenerate with
      <code>python3 -m bench report --json-out</code>
      in
      <code>projects/model-bench</code>.
    </p>
  </footer>
</div>

<style>
  .page {
    max-width: 940px;
    margin: 0 auto;
    padding: 28px 16px 64px;
    background: var(--cream);
    color: var(--ink);
  }

  /* ── Hero ─────────────────────────────── */
  .hero {
    border: 2px solid var(--ink);
    background: var(--paper);
    padding: 24px 22px;
    margin-bottom: 26px;
  }
  .hero-mark {
    font-family: var(--mono);
    font-size: 12px;
    letter-spacing: 0.18em;
    text-transform: uppercase;
    display: inline-block;
    background: var(--accent);
    border: 2px solid var(--ink);
    padding: 3px 8px;
    margin-bottom: 12px;
  }
  .hero h1 {
    font-family: var(--mono);
    font-weight: 800;
    font-size: clamp(30px, 6vw, 46px);
    line-height: 1;
    letter-spacing: -0.02em;
    margin-bottom: 12px;
  }
  .lede {
    max-width: 66ch;
    font-size: 15.5px;
    line-height: 1.55;
    color: var(--ink-2);
  }
  .lede strong {
    font-weight: 700;
    color: var(--ink);
  }
  .meta {
    margin-top: 16px;
    font-family: var(--mono);
    font-size: 12.5px;
    color: var(--ink-3);
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
  }
  .meta .dot {
    color: var(--rule-2);
  }

  /* ── Panels ───────────────────────────── */
  .panel {
    border: 2px solid var(--ink);
    background: var(--paper);
    margin-bottom: 26px;
  }
  .panel-head {
    font-family: var(--mono);
    font-size: 13px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.14em;
    padding: 12px 16px;
    border-bottom: 2px solid var(--ink);
    background: var(--bg-elev);
  }
  .panel-hint {
    float: right;
    font-family: var(--mono);
    font-size: 10.5px;
    font-weight: 400;
    letter-spacing: 0.04em;
    text-transform: none;
    color: var(--ink-3);
  }

  /* ── Table ────────────────────────────── */
  .scroll {
    overflow-x: auto;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 14px;
  }
  thead th {
    font-family: var(--mono);
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--ink-3);
    text-align: right;
    padding: 10px 14px;
    border-bottom: 2px solid var(--ink);
    white-space: nowrap;
  }
  thead th.rk,
  thead th.mdl {
    text-align: left;
  }
  tbody td {
    padding: 12px 14px;
    border-bottom: 1px solid var(--rule);
    text-align: right;
    vertical-align: middle;
  }
  tbody tr:last-child td {
    border-bottom: none;
  }
  td.rk {
    font-family: var(--mono);
    font-weight: 700;
    color: var(--ink-3);
    text-align: left;
    width: 34px;
  }
  td.mdl {
    text-align: left;
  }
  .name {
    font-weight: 700;
    font-size: 14.5px;
    display: block;
    line-height: 1.15;
  }
  .slug {
    font-family: var(--mono);
    font-size: 11px;
    color: var(--ink-3);
  }
  .mono {
    font-family: var(--mono);
  }

  tr.winner td {
    background: var(--accent);
  }
  tr.winner td.rk {
    color: var(--ink);
  }
  /* Claude anchors: the paid baseline you are deciding whether to replace. */
  tr.anchor-row:not(.winner) td.rk {
    box-shadow: inset 3px 0 0 var(--blue);
  }

  /* Expandable rows: the hover/highlight is now a real affordance (click or Enter
     opens the per-task deep-dive) rather than a dead visual. */
  tr.expandable {
    cursor: pointer;
  }
  tr.expandable:not(.winner):hover td,
  tr.expandable.open:not(.winner) td {
    background: var(--bg-elev);
  }
  tr.expandable:focus-visible {
    outline: 2px solid var(--ink);
    outline-offset: -2px;
  }
  .chev {
    font-family: var(--mono);
    font-size: 10px;
    color: var(--ink-3);
    margin-right: 4px;
  }
  tr.open .chev {
    color: var(--ink);
  }

  /* Deep-dive detail row */
  .detail-row td {
    padding: 0;
    text-align: left;
    background: var(--cream);
    border-bottom: 1px solid var(--rule);
  }
  .detail {
    padding: 14px 16px 16px;
  }
  .detail-head {
    font-family: var(--mono);
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: var(--ink-3);
    margin-bottom: 10px;
  }
  .detail-sub {
    margin-left: 8px;
    color: var(--ink-2);
  }
  .bd {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .bd li {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 7px 10px;
    background: var(--paper);
    border: 1px solid var(--rule-2);
  }
  .bd-status {
    font-family: var(--mono);
    font-size: 10.5px;
    font-weight: 700;
    letter-spacing: 0.06em;
    border: 1.5px solid var(--ink);
    padding: 1px 6px;
    flex-shrink: 0;
    width: 46px;
    text-align: center;
  }
  .bd-status.pass {
    background: var(--green);
  }
  .bd-status.fail {
    background: var(--coral);
    color: var(--paper);
  }
  .bd-main {
    display: flex;
    align-items: center;
    gap: 6px;
    min-width: 0;
    flex: 1;
  }
  .bd-id {
    font-family: var(--mono);
    font-size: 12.5px;
    font-weight: 700;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .bd-nums {
    display: flex;
    gap: 12px;
    margin-left: auto;
    font-size: 11.5px;
    color: var(--ink-3);
    flex-shrink: 0;
  }

  /* pass-rate cell */
  .rate {
    font-family: var(--mono);
    font-weight: 700;
    font-size: 15px;
  }
  .rate.full {
    color: var(--teal);
  }
  .rate.partial {
    color: var(--ink-2);
  }
  .rate.zero {
    color: var(--coral);
  }

  /* tool-use pill */
  .pill {
    font-family: var(--mono);
    font-size: 10.5px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    border: 2px solid var(--ink);
    padding: 2px 7px;
    display: inline-block;
  }
  .pill.ok {
    background: var(--green);
  }
  .pill.flaky {
    background: var(--accent);
  }
  .pill.none {
    background: var(--coral);
    color: var(--paper);
  }

  /* tags */
  .tag {
    font-family: var(--mono);
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    border: 1.5px solid var(--ink);
    padding: 1px 5px;
    margin-left: 6px;
    vertical-align: middle;
  }
  .tag.anchor {
    background: var(--blue);
  }
  .tag.real {
    background: var(--green);
  }
  .tag.synth {
    background: var(--bg-elev);
    color: var(--ink-2);
  }
  .tag.tier-easy {
    background: var(--bg-elev);
    color: var(--ink-3);
  }
  .tag.tier-standard {
    background: var(--blue);
  }
  .tag.tier-hard {
    background: var(--accent);
  }

  /* Disqualified table: the failed-floor-tasks column + muted rows. */
  th.fail,
  td.fail {
    text-align: left;
  }
  td.fail {
    font-family: var(--mono);
    font-size: 11.5px;
    color: var(--ink-3);
    line-height: 1.4;
  }
  tr.dq .name {
    color: var(--ink-2);
  }

  .legend {
    display: flex;
    flex-wrap: wrap;
    gap: 6px 18px;
    padding: 12px 16px;
    border-top: 2px solid var(--ink);
    font-size: 12px;
    color: var(--ink-3);
    background: var(--bg-elev);
  }
  .legend b {
    color: var(--ink-2);
  }

  /* ── Tasks ────────────────────────────── */
  .tasks {
    padding: 6px 0;
  }
  .tasks li {
    padding: 13px 16px;
    border-bottom: 1px solid var(--rule);
  }
  .tasks li:last-child {
    border-bottom: none;
  }
  .t-top {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .t-id {
    font-family: var(--mono);
    font-weight: 700;
    font-size: 13.5px;
  }
  .t-score {
    margin-left: auto;
    color: var(--ink-3);
    font-size: 13px;
  }
  .t-blurb {
    margin-top: 4px;
    font-size: 13.5px;
    color: var(--ink-2);
    line-height: 1.45;
  }

  /* ── Note ─────────────────────────────── */
  .note {
    font-size: 13px;
    line-height: 1.6;
    color: var(--ink-3);
    max-width: 72ch;
  }
  .note code {
    font-family: var(--mono);
    font-size: 12px;
    background: var(--bg-elev);
    border: 1px solid var(--rule-2);
    padding: 0 4px;
  }

  @media (max-width: 560px) {
    .slug {
      display: none;
    }
    .hero {
      padding: 20px 16px;
    }
    .bd li {
      flex-wrap: wrap;
    }
    .bd-nums {
      margin-left: 0;
      width: 100%;
    }
  }
</style>
