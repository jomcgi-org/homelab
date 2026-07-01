<script>
  let { data } = $props();

  const lb = $derived(data.leaderboard ?? {});
  const models = $derived(lb.models ?? []);
  const tasks = $derived(lb.tasks ?? []);
  const realTestCount = $derived(tasks.filter((t) => t.real_test).length);

  const pct = (x) => `${Math.round((x ?? 0) * 100)}%`;
  const num = (x) => (x ?? 0).toLocaleString("en-US");
  const money = (x) => `$${(x ?? 0).toFixed(4)}`;
  // Drop the provider prefix for the headline name, keep the full slug beneath.
  const shortName = (id) => (id.includes("/") ? id.split("/").slice(1).join("/") : id);

  // Colour a pass-rate cell: full green, partial coral-ish, zero muted.
  const rateClass = (r) => (r >= 0.999 ? "full" : r > 0 ? "partial" : "zero");
  const toolLabel = (t) =>
    t >= 0.999 ? "ok" : t > 0 ? "flaky" : "none";
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
      An <strong>agentic</strong> coding benchmark over this homelab's real monolith.
      Each model is dropped into a snapshot of the repo with file tools and has to
      make the change itself over multiple turns; it is then graded by the repo's
      <strong>own tests</strong>. The question is not raw capability but which cheap,
      self-hostable models can actually do the offloadable work, ranked by
      reliability, token efficiency, and cost.
    </p>
    <div class="meta">
      <span>{models.length} models</span>
      <span class="dot">·</span>
      <span>{tasks.length} tasks ({realTestCount} real-test)</span>
      <span class="dot">·</span>
      <span>harness {lb.harness_version}</span>
      <span class="dot">·</span>
      <span>{lb.generated_at}</span>
    </div>
  </header>

  <section class="panel">
    <div class="panel-head">Agentic ranking</div>
    <div class="scroll">
      <table>
        <thead>
          <tr>
            <th class="rk">#</th>
            <th class="mdl">Model</th>
            <th class="n">Pass</th>
            <th class="n">Tokens</th>
            <th class="n">Turns</th>
            <th class="n">Cost</th>
            <th class="n">Tools</th>
          </tr>
        </thead>
        <tbody>
          {#each models as m, i}
            <tr class:winner={i === 0}>
              <td class="rk">{i + 1}</td>
              <td class="mdl">
                <span class="name">{shortName(m.id)}</span>
                <span class="slug">{m.id}</span>
                {#if m.role === "anchor"}<span class="tag anchor">anchor</span>{/if}
              </td>
              <td class="n">
                <span class="rate {rateClass(m.pass_rate)}">{pct(m.pass_rate)}</span>
                <span class="bar"><i style="width:{Math.round(m.pass_rate * 100)}%"></i></span>
              </td>
              <td class="n mono">{num(m.median_tokens)}</td>
              <td class="n mono">{m.median_turns}</td>
              <td class="n mono">{money(m.cost_usd)}</td>
              <td class="n">
                <span class="pill {toolLabel(m.tool_use_ok)}">{toolLabel(m.tool_use_ok)}</span>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
    <div class="legend">
      <span><b>Pass</b> share of tasks solved (first attempt).</span>
      <span><b>Tokens / Turns</b> median per task — the efficiency lever.</span>
      <span><b>Cost</b> mean $ per task at list price.</span>
      <span><b>Tools</b> native tool-calling reliability.</span>
    </div>
  </section>

  <section class="panel">
    <div class="panel-head">The tasks</div>
    <ul class="tasks">
      {#each tasks as t}
        <li>
          <div class="t-top">
            <span class="t-id">{t.id}</span>
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
      SWE-bench style: each task snapshots the <em>parent</em> of a real fix commit
      (the buggy state), the model edits the snapshot through file tools, and the
      fix commit's gold test is run against the result on a vendored venv. A "repo
      test" task is graded by the monolith's own pytest suite; a "behavioural" one
      by a hand-written check. Model calls run through OpenRouter at list price.
      Regenerate with <code>python3 -m bench report --json-out</code> in
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
    box-shadow: var(--shadow-hard-lg);
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
  .lede strong { font-weight: 700; color: var(--ink); }
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
  .meta .dot { color: var(--rule-2); }

  /* ── Panels ───────────────────────────── */
  .panel {
    border: 2px solid var(--ink);
    background: var(--paper);
    box-shadow: var(--shadow-hard);
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

  /* ── Table ────────────────────────────── */
  .scroll { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
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
  thead th.rk, thead th.mdl { text-align: left; }
  tbody td {
    padding: 12px 14px;
    border-bottom: 1px solid var(--rule);
    text-align: right;
    vertical-align: middle;
  }
  tbody tr:last-child td { border-bottom: none; }
  td.rk {
    font-family: var(--mono);
    font-weight: 700;
    color: var(--ink-3);
    text-align: left;
    width: 34px;
  }
  td.mdl { text-align: left; }
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
  .mono { font-family: var(--mono); }

  tr.winner td { background: var(--accent); }
  tr.winner td.rk { color: var(--ink); }
  tr:not(.winner):hover td { background: var(--bg-elev); }

  /* pass-rate cell */
  .rate { font-family: var(--mono); font-weight: 700; font-size: 15px; }
  .rate.full { color: var(--teal); }
  .rate.partial { color: var(--ink-2); }
  .rate.zero { color: var(--coral); }
  .bar {
    display: block;
    height: 5px;
    margin-top: 4px;
    background: var(--cream);
    border: 1px solid var(--ink);
    min-width: 56px;
    margin-left: auto;
  }
  .bar i { display: block; height: 100%; background: var(--ink); }
  tr.winner .bar { background: var(--paper); }

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
  .pill.ok { background: var(--green); }
  .pill.flaky { background: var(--accent); }
  .pill.none { background: var(--coral); color: var(--paper); }

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
  .tag.anchor { background: var(--blue); }
  .tag.real { background: var(--green); }
  .tag.synth { background: var(--bg-elev); color: var(--ink-2); }

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
  .legend b { color: var(--ink-2); }

  /* ── Tasks ────────────────────────────── */
  .tasks { padding: 6px 0; }
  .tasks li {
    padding: 13px 16px;
    border-bottom: 1px solid var(--rule);
  }
  .tasks li:last-child { border-bottom: none; }
  .t-top { display: flex; align-items: center; gap: 8px; }
  .t-id { font-family: var(--mono); font-weight: 700; font-size: 13.5px; }
  .t-score { margin-left: auto; color: var(--ink-3); font-size: 13px; }
  .t-blurb { margin-top: 4px; font-size: 13.5px; color: var(--ink-2); line-height: 1.45; }

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
    .slug { display: none; }
    .hero { padding: 20px 16px; }
  }
</style>
