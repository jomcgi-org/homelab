<script>
  import { onMount } from "svelte";
  import { goto } from "$app/navigation";
  import { page } from "$app/stores";
  import FactorySheet from "./FactorySheet.svelte";
  import { relativeTime } from "./execution/run-history.js";
  import {
    NODE_STATE_WORD,
    RECEIPT_STATE_WORD,
    budgetShare,
    conductorHref,
    conductorModel,
    deadlineLabel,
    money,
    phaseLabel,
    planRanks,
    turnShare,
  } from "./factory-view.js";

  let { data } = $props();

  const POLL_MS = 20000;

  // The load result seeds mutable state on purpose: the page then owns the
  // board through its own polling, and a later navigation remounts it.
  // svelte-ignore state_referenced_locally
  let board = $state(data.board);
  // svelte-ignore state_referenced_locally
  let unavailable = $state(data.error);
  // svelte-ignore state_referenced_locally
  let openTask = $state(data.task);
  let now = $state(Date.now());
  let timer = null;

  const active = $derived(board?.active ?? []);
  const queued = $derived(board?.queued ?? []);
  const recent = $derived(board?.recent ?? []);
  const policy = $derived(board?.policy ?? null);
  const intake = $derived(board?.intake ?? null);
  const problemIssues = $derived(board?.problem_issues ?? null);
  const lanes = $derived(board?.lanes ?? null);
  const routing = $derived(board?.review_routing ?? null);
  // Only worth a word when review is not on the model the policy asked for,
  // or when the last thing recorded was a reading nobody could get. Reviewing
  // on Opus with a quiet window is the ordinary state and says nothing.
  const routingLine = $derived(
    routing?.action === "review_waiting"
      ? "review waiting: no reviewer has quota"
      : routing?.action === "reviewer_fallback"
        ? routing.last_action === "quota_guard_unknown" ||
          routing.used_percent == null
          ? `review on ${routing.model}: quota unavailable, retaining previous fallback`
          : `review on ${routing.model}: claude 7d ${Math.round(routing.used_percent)}% used (fallback threshold ${routing.pause_percent}%)`
        : routing?.last_action === "quota_guard_unknown"
          ? "review routing: no reading"
          : null,
  );
  // "1/1 delivery · 0/2 advisory". A lane the policy shut is still shown, so
  // an operator reads a quiet advisory lane as closed rather than as idle.
  const laneLine = $derived(
    lanes
      ? ["delivery", "advisory"]
          .filter((lane) => lanes[lane])
          .map((lane) => `${lanes[lane].active}/${lanes[lane].limit} ${lane}`)
          .join(" · ")
      : "lanes unknown",
  );
  const intakeSeen = $derived(
    intake?.last_admitted?.created_at ?? intake?.last_idle?.created_at ?? null,
  );
  const stateWord = $derived(
    board?.state ?? (unavailable ? "unavailable" : "loading"),
  );
  // Escalations waiting on a person. Shown on the tab rather than as a stats
  // cell: it is a thing to go and do, not a thing about this board.
  const waiting = $derived(
    (board?.escalations ?? []).filter((item) => item.open).length,
  );

  async function refresh() {
    try {
      const query = openTask ? `?task=${encodeURIComponent(openTask)}` : "";
      const response = await fetch(`/factory${query}`);
      if (!response.ok) throw new Error("factory unavailable");
      board = await response.json();
      unavailable = false;
      now = Date.now();
    } catch {
      unavailable = true;
    }
  }

  onMount(() => {
    timer = setInterval(() => {
      if (document.visibilityState === "visible") refresh();
    }, POLL_MS);
    const tick = setInterval(() => (now = Date.now()), 30000);
    return () => {
      clearInterval(timer);
      clearInterval(tick);
    };
  });

  async function toggle(receipt) {
    const next = openTask === receipt.task_id ? null : receipt.task_id;
    openTask = next;
    const params = new URLSearchParams($page.url.search);
    if (next) params.set("task", next);
    else params.delete("task");
    const search = params.toString();
    await goto(
      search ? `${$page.url.pathname}?${search}` : $page.url.pathname,
      { replaceState: true, noScroll: true, keepFocus: true },
    );
    if (next && !receipt.nodes?.length) await refresh();
  }

  function receiptWord(receipt) {
    return RECEIPT_STATE_WORD[receipt.state] ?? receipt.state;
  }

  function sessionHref(id) {
    return `/factory/execution?session=${encodeURIComponent(id)}`;
  }

  function attemptWord(attempt) {
    const parts = [attempt.status];
    if (attempt.session?.model) parts.push(attempt.session.model);
    if (attempt.cost_usd != null) parts.push(money(attempt.cost_usd));
    return parts.join(" · ");
  }

  function pct(share) {
    return `${Math.round(share * 100)}%`;
  }

  function startName(start) {
    return start.start_key.split(":").slice(-2).join(":");
  }
</script>

<svelte:head>
  <title>Factory</title>
</svelte:head>

<FactorySheet className="factory-page" view="factory" {waiting}>
  <h1 class="sr-only">Factory</h1>

  <section class="stats edition" aria-label="Factory state">
    <div>
      <span class="k">state</span>
      <span class={`v state-${stateWord}`}>{stateWord}</span>
    </div>
    <div>
      <span class="k">in flight</span>
      <span class="v num">{active.length}</span>
    </div>
    <div>
      <span class="k">queued</span>
      <span class="v num">{queued.length}</span>
    </div>
    <div>
      <span class="k">plans</span>
      <span class="v">{policy?.conductor_model ?? "–"}</span>
    </div>
    <div>
      <span class="k">builds</span>
      <span class="v">{policy?.worker_model ?? "–"}</span>
    </div>
    <div>
      <span class="k">reviews</span>
      <span class="v">{policy?.reviewer_model ?? "–"}</span>
    </div>
    <div>
      <span class="k">intake</span>
      <span class="v"
        >{intake?.policy?.enabled
          ? `${intake.admitted_today}/${intake.max_per_day} today`
          : "off"}</span
      >
    </div>
    <div>
      <span class="k">problem issues</span>
      <span class="v"
        >{problemIssues?.status === "on"
          ? `${problemIssues.writes_started_today}/${problemIssues.max_per_24_hours} writes / 24h`
          : "off"}</span
      >
    </div>
  </section>

  <p class="policy-line">
    {#if policy}
      <span
        >generation {policy.generation} · {laneLine} · {money(
          policy.task_budget_usd,
        )} and up to {policy.max_task_turns_hard} starts per task · {policy.max_parallel_nodes ??
          1} in parallel · policy v{board.version}</span
      >
    {/if}
    {#if routingLine}
      <span class="guard">{routingLine}</span>
    {/if}
    {#if intake?.policy?.enabled && intakeSeen}
      <span
        >last intake {intake.last_admitted
          ? `admitted #${intake.last_admitted.detail?.issue_number ?? "?"}`
          : "found nothing"} at {new Date(
          intakeSeen,
        ).toLocaleTimeString()}</span
      >
    {/if}
    {#if problemIssues?.last_event}
      <span
        >problem issues {problemIssues.last_event.action.replaceAll("_", " ")} at
        {new Date(
          problemIssues.last_event.created_at,
        ).toLocaleTimeString()}</span
      >
    {/if}
    {#if board && board.ok === false}
      <span class="warn">factory {board.reason ?? "not ready"}</span>
    {/if}
    {#if unavailable}
      <span class="warn">board unavailable, showing the last read</span>
    {/if}
    <a class="talk" href={conductorHref(null, board)}
      >Talk to the conductor ({conductorModel(null, board)})</a
    >
  </p>

  <section>
    <p class="sec-label">
      / In flight <span class="num">{active.length}</span>
    </p>
    {#if active.length === 0}
      <p class="none">Nothing in flight.</p>
    {/if}
    {#each active as receipt (receipt.id)}
      {@render card(receipt, true)}
    {/each}
  </section>

  <section>
    <p class="sec-label">/ Queue <span class="num">{queued.length}</span></p>
    {#if queued.length === 0}
      <p class="none">Queue is empty. The next issue in the policy feeds it.</p>
    {/if}
    {#each queued as receipt (receipt.id)}
      {@render card(receipt, false)}
    {/each}
  </section>

  <section>
    <p class="sec-label">/ Recent <span class="num">{recent.length}</span></p>
    {#if recent.length === 0}
      <p class="none">No finished tasks yet.</p>
    {/if}
    {#each recent as receipt (receipt.id)}
      {@render card(receipt, false)}
    {/each}
  </section>
</FactorySheet>

{#snippet card(receipt, inFlight)}
  {@const open = Boolean(receipt.task_id) && openTask === receipt.task_id}
  <article class={`panel edition ${receipt.state}`} class:open>
    <button
      type="button"
      class="panel-head ed-head"
      aria-expanded={open}
      disabled={!receipt.task_id}
      onclick={() => toggle(receipt)}
    >
      <span class="issue code">#{receipt.issue_number}</span>
      <span class="title">{receipt.title}</span>
      <span class={`badge code state-${receipt.state}`}
        >{receiptWord(receipt)}</span
      >
    </button>
    <dl class="spec">
      <div class="row">
        <dt class="label">phase</dt>
        <dd class="value">
          <span
            class={`dot ${inFlight && !receipt.task_paused ? "running" : ""}`}
          ></span>
          {phaseLabel(receipt)}
          {#if inFlight}<span class="soft">
              · {deadlineLabel(receipt, now)}</span
            >{/if}
        </dd>
      </div>
      {#if receipt.task_id}
        <div class="row">
          <dt class="label">starts</dt>
          <dd class="value meter">
            <span class="bar"
              ><i style={`width:${pct(turnShare(receipt))}`}></i></span
            >
            <span class="num"
              >{receipt.turns_used} of {receipt.allowance?.turns ??
                receipt.policy
                  .max_task_turns_hard}{#if receipt.planner_turns_used}
                + {receipt.planner_turns_used} planning{/if}</span
            >
          </dd>
        </div>
        <div class="row">
          <dt class="label">spend</dt>
          <dd class="value meter">
            <span class="bar"
              ><i style={`width:${pct(budgetShare(receipt))}`}></i></span
            >
            <span class="num"
              >{money(receipt.committed_cost_usd)} of {money(
                receipt.policy.task_budget_usd,
              )}</span
            >
          </dd>
        </div>
      {/if}
      {#if receipt.evidence?.pr_url}
        <div class="row">
          <dt class="label">pr</dt>
          <dd class="value">
            <a href={receipt.evidence.pr_url}>{receipt.evidence.pr_url}</a>
          </dd>
        </div>
      {/if}
    </dl>
    {#if receipt.nodes?.length}
      {@render dag(receipt)}
    {/if}
    {#if open}
      {@render detail(receipt)}
    {/if}
  </article>
{/snippet}

{#snippet dag(receipt)}
  <ol class="dag" aria-label="plan">
    {#each planRanks(receipt.nodes) as rank, rankIndex (rankIndex)}
      <li class="rank">
        {#each rank as node (node.key)}
          <div class={`node ${node.state}`}>
            <span class="nh">
              <span class={`dot ${node.state}`} aria-hidden="true"></span>
              <span>{node.label}</span>
            </span>
            <span class="nm code">
              <span>{node.model ?? "policy"}</span>
              <span>{NODE_STATE_WORD[node.state] ?? node.state}</span>
            </span>
          </div>
        {/each}
      </li>
    {/each}
  </ol>
{/snippet}

{#snippet detail(receipt)}
  <div class="detail">
    <p class="actions">
      <a class="talk" href={conductorHref(receipt)}
        >Discuss with the conductor</a
      >
      <a href={receipt.url}>issue on GitHub</a>
    </p>

    <p class="sec-label">/ Nodes</p>
    <ol class="nodes">
      {#each receipt.nodes as node (node.node_key)}
        <li>
          <div class="node-row">
            <span class={`dot ${node.state}`} aria-hidden="true"></span>
            <span class="name">{node.label}</span>
            <span class="code soft"
              >{node.kind} · {node.model ?? "policy"} · {NODE_STATE_WORD[
                node.state
              ] ?? node.state}{#if node.deps.length}
                · after {node.deps.join(", ")}{/if}</span
            >
          </div>
          {#each node.attempts as attempt (attempt.attempt)}
            <div class="attempt">
              <span class="code soft"
                >attempt {attempt.attempt} · {attemptWord(attempt)}</span
              >
              {#if attempt.session}
                <a class="session" href={sessionHref(attempt.session.id)}>
                  <span class="code">session {attempt.session.id}</span>
                  <span class="code soft">
                    {attempt.session.status}{#if attempt.session.guest_bound}
                      · guest{/if}{#if attempt.session.last_turn_at}
                      · {relativeTime(attempt.session.last_turn_at, now)}{/if}
                  </span>
                  {#if attempt.session.result_head}
                    <span class="head">{attempt.session.result_head}</span>
                  {/if}
                </a>
              {/if}
            </div>
          {/each}
        </li>
      {/each}
    </ol>

    {#if receipt.starts?.length}
      <p class="sec-label">/ Starts</p>
      <ol class="ledger code">
        {#each receipt.starts as start (start.start_key)}
          <li>
            <span>{startName(start)}</span>
            <span>{start.model}</span>
            <span class={`state-${start.status}`}>{start.status}</span>
            <span class="num"
              >{start.cost_usd != null ? money(start.cost_usd) : ""}</span
            >
          </li>
        {/each}
      </ol>
    {/if}

    {#if receipt.stop_events?.length}
      <p class="sec-label">/ Stop events</p>
      <ol class="ledger code">
        {#each receipt.stop_events as event, index (index)}
          <li>
            <span>{event.action}</span>
            <span>{event.reason ?? ""}</span>
            <span class={event.intervention_required ? "state-uncertain" : ""}
              >{event.intervention_required ? "needs you" : ""}</span
            >
            <span>{relativeTime(event.created_at, now)}</span>
          </li>
        {/each}
      </ol>
    {/if}

    {#if receipt.evidence?.reason}
      <p class="sec-label">/ Evidence</p>
      <p class="code soft evidence">{receipt.evidence.reason}</p>
    {/if}
  </div>
{/snippet}

<style>
  .code {
    font-family: var(--font-code);
  }
  .soft {
    color: var(--ink-2);
  }
  a {
    color: var(--accent-ink);
  }
  a:focus-visible,
  button:focus-visible {
    outline: 2px solid var(--accent-ink);
    outline-offset: 3px;
  }

  /* Masthead: the app chrome the slop/factory page wears, one boxed strip
     of crumbs and one of view tabs, both partitioned by --stroke. */
  .stats {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    border: 1px solid var(--ink);
  }
  .stats > div {
    min-width: 0;
    padding: 0.55rem 0.8rem 0.6rem;
    border-right: 1px solid var(--stroke);
  }
  .stats > div:nth-child(3n) {
    border-right: 0;
  }
  .stats > div:nth-child(-n + 3) {
    border-bottom: 1px solid var(--stroke);
  }
  .stats .k {
    display: block;
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.66rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }
  .stats .v {
    display: block;
    margin-top: 0.3em;
    font-size: 1.1rem;
    font-weight: 700;
    letter-spacing: -0.02em;
    line-height: 1.1;
    overflow-wrap: anywhere;
  }
  .stats .v.state-enabled {
    color: var(--ok);
  }
  .stats .v.state-paused,
  .stats .v.state-unavailable {
    color: var(--warn);
  }
  .stats .v.state-stopped,
  .stats .v.state-disabled {
    color: var(--ds-err);
  }

  .policy-line {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.5rem 1rem;
    margin: 0.75rem 0 2rem;
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.72rem;
  }
  .policy-line .warn {
    color: var(--warn);
  }
  .policy-line .guard {
    color: var(--warn);
  }
  .talk {
    display: inline-flex;
    align-items: center;
    min-height: 2.75rem;
    margin-left: auto;
    padding: 0 1em;
    border: 1px solid var(--ink);
    color: var(--ink);
    font-family: var(--font-ui);
    font-size: 0.85rem;
    font-weight: 600;
    text-decoration: none;
  }
  .talk:hover {
    background: var(--band);
  }

  section {
    margin-bottom: 2.25rem;
  }
  .none {
    margin: 0;
    color: var(--ink-2);
    font-size: 0.9rem;
  }

  /* Panels: one 1px ink outline per task, partitioned edge to edge. */
  .panel {
    margin-bottom: 0.9rem;
  }
  .panel-head {
    width: 100%;
    min-height: 2.75rem;
    border-top: 0;
    border-left: 0;
    border-right: 0;
    text-align: left;
    cursor: pointer;
  }
  .panel-head:disabled {
    cursor: default;
  }
  .issue {
    color: var(--ink-2);
    font-size: 0.78rem;
  }
  .title {
    flex: 1 1 14em;
    min-width: 0;
    font-size: 0.95rem;
    font-weight: 700;
    letter-spacing: -0.01em;
  }
  .badge {
    padding: 0.1em 0.55em;
    border: 1px solid var(--stroke);
    color: var(--ink-2);
    font-size: 0.66rem;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    white-space: nowrap;
  }
  .badge.state-admitted {
    border-color: var(--ok);
    color: var(--ok);
  }
  .badge.state-uncertain {
    border-color: var(--warn);
    color: var(--warn);
  }
  .badge.state-failed,
  .badge.state-cancelled {
    border-color: var(--ds-err);
    color: var(--ds-err);
  }

  .value {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.4rem;
    min-width: 0;
  }
  .meter .bar {
    flex: 1 1 6em;
    height: 4px;
    background: var(--band);
  }
  .meter .bar i {
    display: block;
    height: 100%;
    background: var(--ink);
  }
  .meter .num {
    font-family: var(--font-code);
    font-size: 0.72rem;
  }

  .dot {
    flex: none;
    width: 0.5rem;
    height: 0.5rem;
    border: 1px solid var(--ink);
    box-sizing: border-box;
  }
  .dot.running {
    border-color: var(--ok);
    background: var(--ok);
  }
  .dot.done {
    background: var(--ink);
  }
  .dot.failed,
  .dot.cancelled {
    border-color: var(--ds-err);
    background: var(--ds-err);
  }
  .dot.uncertain {
    border-color: var(--warn);
    background: var(--warn);
  }
  .dot.retired {
    border-color: var(--ink-2);
    background: transparent;
  }

  /* The plan: an operating sequence, stages left to right on a desk and a
     numbered rail top to bottom on a phone, one outline per part. */
  .dag {
    display: flex;
    flex-direction: column;
    margin: 0;
    padding: 0.8rem;
    border-top: 1px solid var(--stroke);
    list-style: none;
    counter-reset: stage;
  }
  .rank {
    position: relative;
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
    padding: 0 0 0.6rem 1.6rem;
    counter-increment: stage;
  }
  .rank::before {
    position: absolute;
    top: 0.2rem;
    left: 0;
    width: 1rem;
    height: 1rem;
    border: 1px solid var(--ink);
    color: var(--ink-2);
    font: 0.6rem / 1rem var(--font-code);
    text-align: center;
    content: counter(stage);
  }
  .rank::after {
    position: absolute;
    top: 1.4rem;
    bottom: 0;
    left: 0.5rem;
    border-left: 1px dashed var(--stroke);
    content: "";
  }
  .rank:last-child::after {
    display: none;
  }
  .node {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 0.2rem 0.7rem;
    padding: 0.25rem 0;
  }
  .nh {
    display: flex;
    align-items: center;
    gap: 0.45rem;
    font-size: 0.85rem;
    font-weight: 600;
  }
  .nm {
    display: flex;
    gap: 0.6rem;
    color: var(--ink-2);
    font-size: 0.68rem;
  }
  .node.running .nh {
    color: var(--ok);
  }
  .node.failed .nh,
  .node.cancelled .nh {
    color: var(--ds-err);
  }
  .node.uncertain .nh {
    color: var(--warn);
  }
  .node.pending .nh,
  .node.retired .nh {
    color: var(--ink-2);
    font-weight: 500;
  }

  @media (min-width: 900px) {
    .stats {
      grid-template-columns: repeat(4, minmax(0, 1fr));
    }
    .stats > div:nth-child(3n) {
      border-right: 1px solid var(--stroke);
    }
    .stats > div:last-child {
      border-right: 0;
    }
    .stats > div:nth-child(-n + 3) {
      border-bottom: 0;
    }
    .stats .v {
      font-size: 1.45rem;
    }
    .dag {
      flex-direction: row;
      align-items: stretch;
      overflow-x: auto;
    }
    .rank {
      flex: 0 0 auto;
      width: 13em;
      padding: 1.5rem 0.8rem 0.4rem 0;
    }
    .rank::before {
      top: 0;
      left: 0;
    }
    .rank::after {
      top: 0.5rem;
      right: 0;
      bottom: auto;
      left: 1.3rem;
      border-top: 1px dashed var(--stroke);
      border-left: 0;
    }
    .node {
      flex-direction: column;
      align-items: stretch;
      gap: 0.3rem;
      padding: 0.5rem 0.6rem;
      border: 1px solid var(--stroke);
    }
    .node.running {
      border-color: var(--ok);
    }
  }

  /* Detail */
  .detail {
    padding: 0.8rem;
    border-top: 1px solid var(--stroke);
  }
  .actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.6rem 1.2rem;
    margin: 0 0 1.2rem;
    font-size: 0.85rem;
  }
  .actions .talk {
    margin-left: 0;
  }
  .nodes,
  .ledger {
    margin: 0 0 1.4rem;
    padding: 0;
    list-style: none;
  }
  .nodes > li {
    padding: 0.5rem 0;
    border-bottom: 1px solid var(--line);
  }
  .node-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.3rem 0.6rem;
    font-size: 0.85rem;
  }
  .node-row .name {
    font-weight: 600;
  }
  .node-row .code,
  .attempt .code {
    font-size: 0.68rem;
  }
  .attempt {
    margin: 0.4rem 0 0 1.1rem;
  }
  .session {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 0.2rem 0.7rem;
    min-height: 2.75rem;
    margin-top: 0.3rem;
    padding: 0.45rem 0.6rem;
    border: 1px solid var(--stroke);
    color: var(--ink);
    text-decoration: none;
  }
  .session:hover {
    background: var(--band);
  }
  .session .head {
    flex-basis: 100%;
    color: var(--ink-2);
    font-size: 0.8rem;
    overflow-wrap: anywhere;
  }
  .ledger li {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 0.7rem;
    padding: 0.35rem 0;
    border-bottom: 1px solid var(--line);
    font-size: 0.72rem;
    overflow-wrap: anywhere;
  }
  .state-succeeded {
    color: var(--ok);
  }
  .state-failed {
    color: var(--ds-err);
  }
  .state-uncertain,
  .state-reserved {
    color: var(--warn);
  }
  .evidence {
    margin: 0;
    font-size: 0.72rem;
    overflow-wrap: anywhere;
  }
</style>
