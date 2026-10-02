<script>
  // Every selected model against every task, one square per cell. Columns are
  // numbered and keyed to the task list below rather than labelled, the way a
  // figure keys its parts: seventeen task ids across the top would not fit.
  // Mark language follows the factory: filled ink is done, a red square failed,
  // hatch is errored before grading (does not count), an empty dot never ran.
  import { cellState, orderedTasks, providerSlot, shortName } from "./model.js";

  let { models = [], tasks = [], hot = null, onhover = () => {} } = $props();

  const cols = $derived(orderedTasks(tasks));
  const tiers = $derived(
    ["easy", "standard", "hard"]
      .map((tier) => ({
        tier,
        span: cols.filter((t) => t.tier === tier).length,
      }))
      .filter((g) => g.span),
  );
  const LABEL = {
    pass: "passed",
    fail: "failed",
    errored: "errored",
    none: "not run",
  };
</script>

<section class="panel matrix">
  <header class="panel-head">
    <span class="t">Per-task results</span>
    <span class="u">columns keyed to the task list below</span>
  </header>
  <div class="panel-body scroll">
    <table class:hovering={hot}>
      <thead>
        <tr class="tiers">
          <th></th>
          {#each tiers as g}
            <th colspan={g.span}>{g.tier}</th>
          {/each}
          <th></th>
        </tr>
        <tr>
          <th class="m">model</th>
          {#each cols as t (t.id)}
            <th
              class="c num"
              class:tier-start={t.no > 1 && cols[t.no - 2].tier !== t.tier}
              title={t.id}><a href={`#task-${t.no}`}>{t.no}</a></th
            >
          {/each}
          <th class="s">solved</th>
        </tr>
      </thead>
      <tbody>
        {#each models as m (m.id)}
          {@const states = cols.map((t) => cellState(m, t.id))}
          <tr
            data-model={m.id}
            class:hot={hot === m.id}
            onmouseenter={() => onhover(m.id)}
            onmouseleave={() => onhover(null)}
          >
            <th class="m" scope="row"
              ><i class="sw" data-slot={providerSlot(m.id)}></i>{shortName(
                m,
              )}</th
            >
            {#each cols as t, i (t.id)}
              <td
                class:tier-start={i > 0 && cols[i - 1].tier !== t.tier}
                title={`${shortName(m)} · ${t.id}: ${LABEL[states[i]]}`}
                ><i class="cell {states[i]}" aria-label={LABEL[states[i]]}
                ></i></td
              >
            {/each}
            <td class="s num"
              >{states.filter((s) => s === "pass").length}/{states.filter(
                (s) => s === "pass" || s === "fail",
              ).length}</td
            >
          </tr>
        {/each}
      </tbody>
    </table>
  </div>
  <p class="key">
    <span><i class="cell pass"></i>passed</span>
    <span><i class="cell fail"></i>failed</span>
    <span><i class="cell errored"></i>errored before grading, not counted</span>
    <span
      ><i class="cell none"></i>not run (task added after the model's run)</span
    >
  </p>
</section>

<style>
  .scroll {
    overflow-x: auto;
  }

  table {
    border-collapse: collapse;
    font-family: var(--font-code);
    font-size: 0.7rem;
  }

  th {
    font-weight: 500;
  }

  .tiers th {
    padding: 0 0 0.3em;
    color: var(--ink-2);
    font-size: 0.62rem;
    letter-spacing: 0.08em;
    text-align: left;
    text-transform: uppercase;
  }

  .tiers th + th {
    border-left: 1px solid var(--stroke);
    padding-left: 0.4em;
  }

  thead tr:last-child th {
    padding-bottom: 0.3em;
    border-bottom: 1px solid var(--stroke);
    color: var(--ink-2);
  }

  th.c {
    width: 1.7em;
    min-width: 1.7em;
    text-align: center;
  }

  th.c a {
    text-decoration: none;
  }

  th.c a:hover {
    color: var(--accent-ink);
  }

  th.m {
    padding-right: 1em;
    color: var(--ink);
    text-align: left;
    white-space: nowrap;
  }

  th.m .sw {
    margin-right: 0.45em;
  }

  thead th.m {
    color: var(--ink-2);
  }

  .s {
    padding-left: 0.9em;
    text-align: right;
    white-space: nowrap;
  }

  tbody tr + tr > * {
    border-top: 1px solid var(--line);
  }

  tbody th,
  tbody td {
    padding: 0.28em 0;
  }

  td {
    text-align: center;
  }

  .tier-start {
    border-left: 1px solid var(--stroke);
  }

  .cell {
    display: inline-block;
    width: 0.8em;
    height: 0.8em;
    border: 1px solid var(--ink);
    vertical-align: -0.1em;
  }

  .cell.pass {
    background: var(--ink);
  }

  .cell.fail {
    border-color: var(--tone-disk);
    background: var(--tone-disk);
  }

  .cell.errored {
    background: var(--hatch);
  }

  .cell.none {
    width: 0.25em;
    height: 0.25em;
    border: 0;
    background: var(--ink-3);
    vertical-align: 0.1em;
  }

  .key {
    display: flex;
    flex-wrap: wrap;
    gap: 0.35em 1.2em;
    margin: 0;
    padding: 0.5em 0.8em;
    border-top: 1px solid var(--stroke);
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.68rem;
  }

  .key .cell {
    margin-right: 0.45em;
  }

  .key .cell.none {
    margin: 0 0.6em 0 0.25em;
  }
</style>
