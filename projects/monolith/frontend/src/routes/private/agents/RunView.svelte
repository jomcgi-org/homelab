<script>
  import StateIcon from "./StateIcon.svelte";
  import {
    capacityPips,
    computeRanks,
    defaultSelectedKey,
    layoutEdges,
    nodeIconKey,
    nodeStateClass,
  } from "./dag.js";
  import {
    agoPhrase,
    engineStale,
    firstLine,
    fmtCost,
    fmtDur,
    joinMeta,
    queuedOnQueue,
    relSeconds,
    spendOfBudget,
    startedAgo,
    stateFor,
  } from "./run-format.js";
  import PaneHeader from "./PaneHeader.svelte";
  import { crumbTrail } from "./lineage.js";
  import { RUN_LEXICON as P } from "./run-lexicon.js";
  import { claimStatus } from "./claims.js";

  let {
    run,
    view,
    sessions = [],
    onSelectSession = () => {},
    onCancel = () => {},
    onCrumb = () => {},
  } = $props();

  let selectedKey = $state(null);
  let runView = $state("plan");

  const active = $derived(
    run?.dbos_status === "PENDING" || run?.dbos_status === "ENQUEUED",
  );
  const ranks = $derived(computeRanks(run?.nodes ?? []));
  const edges = $derived(layoutEdges(ranks));
  const selectedNode = $derived(
    run?.nodes?.find((node) => node.key === selectedKey) ?? null,
  );
  const hasAside = $derived(
    selectedNode?.blocked_on?.kind === "human" ||
      Boolean(selectedNode?.attempts?.length),
  );
  const needsHuman = $derived(
    run?.nodes?.some(
      (node) => node.state === "escalated" || node.blocked_on?.kind === "human",
    ) ?? false,
  );
  const humanAttention = $derived(
    run?.nodes?.some((node) => node.blocked_on?.kind === "human") ?? false,
  );
  const reviewedNode = $derived(
    run?.nodes?.find((node) => node.verdict?.commit_url) ?? null,
  );
  const fallbackDeviations = $derived(
    (run?.deviations ?? []).filter(
      (deviation) =>
        !run?.nodes?.some((node) => node.key === deviation.node_key),
    ),
  );
  const attemptRows = $derived(
    (run?.nodes ?? [])
      .flatMap((node) =>
        (node.attempts ?? []).map((attempt) => ({ node, attempt })),
      )
      .sort(
        (a, b) =>
          Date.parse(a.attempt.started_at || 0) -
          Date.parse(b.attempt.started_at || 0),
      ),
  );
  const logEntries = $derived(buildLogEntries(run));

  // Every run reuses the same node keys (implement, push_gate, review), so a
  // key surviving the switch is not evidence the selection still applies:
  // recompute the default whenever the run itself changes.
  let selectedRunId = $state(null);
  $effect(() => {
    const nodes = run?.nodes ?? [];
    if (
      run?.workflow_id !== selectedRunId ||
      !nodes.some((node) => node.key === selectedKey)
    ) {
      selectedRunId = run?.workflow_id ?? null;
      selectedKey = defaultSelectedKey(nodes);
    }
  });

  const shortSha = (sha) => String(sha || "").slice(0, 8);
  const shortId = (id) => String(id || "").slice(-12);
  const since = (value) => relSeconds(value, view.now);
  const ago = (value) => agoPhrase(since(value));
  const isHumanGate = (node) => node?.blocked_on?.kind === "human";
  const deviationsForNode = (nodeKey) =>
    (run?.deviations ?? []).filter(
      (deviation) => deviation.node_key === nodeKey,
    );

  function nodeOwner(node) {
    if (isHumanGate(node)) return P.labels.youWord;
    if (node.kind === "merge") return P.labels.rebaseOnly;
    return node.model;
  }

  function nodeTiming(node) {
    if (isHumanGate(node)) {
      return stateFor(
        P.labels.waitingWordLower,
        relSeconds(node.blocked_on?.since ?? run.created_at, view.now),
      );
    }
    if (node.kind === "review" || node.key === "review") {
      return run.plan?.max_review_cycles == null
        ? P.nodeStates[node.state] || node.state
        : `${P.labels.upTo} ${run.plan.max_review_cycles} ${P.labels.cycles}`;
    }
    if (node.attempts?.length) {
      const first = node.attempts[0];
      const last = node.attempts.at(-1);
      const duration = relSeconds(first.started_at, last.ended_at ?? view.now);
      return joinMeta(
        `${node.attempts.length} ${
          node.attempts.length === 1 ? P.labels.attempt : P.labels.attempts
        }`,
        fmtDur(duration),
      );
    }
    return P.nodeStates[node.state] || node.state;
  }

  function detailPill(node) {
    const attempt = node?.attempts?.at(-1);
    return attempt
      ? `${P.labels.attempt} ${attempt.n}`
      : P.nodeStates[node?.state] || node?.state;
  }

  function attemptTitle(node, attempt) {
    return joinMeta(node.label, `${P.labels.attempt} ${attempt.n}`);
  }

  function attemptSummary(attempt) {
    return joinMeta(
      attempt.model,
      P.stateWords[attempt.state] || attempt.state,
      fmtCost(attempt.cost_usd),
    );
  }

  function buildLogEntries(value) {
    if (!value) return [];
    const entries = [];
    for (const node of value.nodes ?? []) {
      if (node.queue) {
        entries.push({ kind: "queue", node, at: value.created_at });
      }
      if (node.blocked_on) {
        entries.push({
          kind: "blocked",
          node,
          at: node.blocked_on.since ?? value.updated_at,
        });
      }
      if (node.decision) {
        entries.push({ kind: "decision", node, at: value.updated_at });
      }
      for (const attempt of node.attempts ?? []) {
        entries.push({
          kind: "attempt",
          node,
          attempt,
          at: attempt.started_at,
        });
      }
      if (node.verdict) {
        entries.push({
          kind: "verdict",
          node,
          at: value.completed_at ?? value.updated_at,
        });
      }
      if (node.evidence) {
        entries.push({ kind: "evidence", node, at: value.updated_at });
      }
      if (node.note) {
        entries.push({ kind: "note", node, at: value.updated_at });
      }
      for (const deviation of deviationsForNode(node.key)) {
        entries.push({
          kind: "deviation",
          node,
          deviation,
          at: deviation.at ?? value.updated_at,
        });
      }
    }
    for (const deviation of (value.deviations ?? []).filter(
      (item) => !value.nodes?.some((node) => node.key === item.node_key),
    )) {
      entries.push({
        kind: "deviation",
        node: { label: P.labels.run },
        deviation,
        at: deviation.at ?? value.updated_at,
      });
    }
    return entries.sort(
      (a, b) => Date.parse(a.at || 0) - Date.parse(b.at || 0),
    );
  }
</script>

<div class:tier-stale={view.engine_tier === "stale"} class="runview">
  {#if !run || view.engine_tier === "absent"}
    <PaneHeader kind={P.labels.run} {onCrumb}>
      {#snippet chips()}
        <span class="state-chip">{P.labels.sessionsOnly}</span>
      {/snippet}
    </PaneHeader>
    <div class="absent-note">{P.labels.absentNotice}</div>
    <div class="sess-list">
      {#each sessions as session}
        <button
          class="sess-row"
          type="button"
          onclick={() => onSelectSession(session.id)}
        >
          <span
            class={`dot ${session.status === "running" ? "running" : session.status === "warn" ? "warn" : ""}`}
            aria-hidden="true"
          ></span>
          <span class="sess-title">{session.title}</span>
          <span class="sess-meta"
            >{joinMeta(
              session.status,
              session.model,
              fmtCost(session.total_cost_usd),
              ago(session.last_turn_at),
            )}</span
          >
        </button>
      {/each}
    </div>
  {:else}
    {@const claim = claimStatus(run, view.now)}
    <header class="run-head">
      <PaneHeader
        runRow
        crumbs={crumbTrail({ kind: "run", runTitle: firstLine(run.task.text) })}
        {onCrumb}
        workflowId={run.workflow_id}
        runActive={active}
        engineTier={view.engine_tier}
        branchUrl={run.branch_url}
        reviewedCommitUrl={reviewedNode?.verdict?.commit_url}
        reviewedCommitSha={reviewedNode?.verdict?.commit_sha}
        {onCancel}
      >
        <h1 class="run-title" title={run.task.text}>
          {firstLine(run.task.text)}
        </h1>
        <span class="seg" role="group" aria-label={P.labels.runViewLabel}>
          <button
            type="button"
            class:selected={runView === "plan"}
            aria-pressed={runView === "plan"}
            onclick={() => (runView = "plan")}>{P.labels.planView}</button
          >
          <button
            type="button"
            class:selected={runView === "log"}
            aria-pressed={runView === "log"}
            onclick={() => (runView = "log")}>{P.labels.logView}</button
          >
        </span>
      </PaneHeader>
    </header>

    <div class="facts" data-register="fact">
      <span class:attention={needsHuman} class="fact-state">
        {#if needsHuman}
          <StateIcon icon="blocked_human" class="g-blocked-h" />
          {P.labels.waitingOnYou}
        {:else}
          {P.stateWords[run.state] || run.state}
        {/if}
      </span>
      {#if run.branch_url}
        <a class="branch-fact" href={run.branch_url}>
          <svg viewBox="0 0 16 16" aria-hidden="true">
            <circle cx="4" cy="3" r="1.5"></circle>
            <circle cx="4" cy="13" r="1.5"></circle>
            <circle cx="12" cy="5" r="1.5"></circle>
            <path d="M4 4.5v5.25A3.25 3.25 0 0 0 7.25 13H10"></path>
            <path d="M4 6.5h4.75A3.25 3.25 0 0 0 12 3.25V3"></path>
          </svg>
          {run.work_branch}
        </a>
      {/if}
      {#if spendOfBudget(run.cost_usd, run.plan?.budget_usd)}
        <span class="mono-fact"
          >{spendOfBudget(run.cost_usd, run.plan?.budget_usd)}</span
        >
      {/if}
      {#if startedAgo(since(run.created_at))}
        <span>{startedAgo(since(run.created_at))}</span>
      {/if}
      {#if view.engine_tier !== "live"}
        <span class="fact-stale">{engineStale(view.snapshot_age_seconds)}</span>
      {/if}
      {#if run.cancelled_by}
        <span>{P.labels.cancelledBy} {run.cancelled_by.actor}</span>
      {/if}
    </div>

    {#if claim.unconfirmed}
      <div class="conflict" data-register="belief">
        {P.labels.stateConflict}
      </div>
    {/if}

    {#if run.disposition}
      <div class:human-attention={humanAttention} class="disposition">
        <div class="disposition-state">
          {P.dispositionStates[run.disposition.state] ?? run.disposition.state}
        </div>
        <div class="disposition-reason">
          <span class="disposition-label">{P.labels.reason}</span>
          {run.disposition.reason}
        </div>
        {#if run.disposition.next}
          <div class="disposition-next">
            <span class="disposition-label">{P.labels.next}</span>
            {run.disposition.next}
          </div>
        {/if}
      </div>
    {/if}

    {#if String(run.task.text ?? "").includes("\n")}
      <details class="rv-task-details">
        <summary>{P.labels.fullTask}</summary>
        <pre>{run.task.text}</pre>
      </details>
    {/if}
    {#if run.stranded}
      <div class="banner" data-register="belief">
        <span class="register-tag">{P.labels.engineBelief}</span>
        {joinMeta(
          P.labels.strandedBanner,
          run.app_version == null
            ? null
            : `${P.labels.buildWord} ${run.app_version}`,
          run.server_app_version == null
            ? null
            : `${P.labels.serverBuildWord} ${run.server_app_version}`,
        )}
      </div>
    {/if}
    {#if claim.terminal && claim.observation}
      <div class="obs-line" data-register="fact">
        <span class="lbl">{P.labels.lastActivity}</span>
        <span
          >{joinMeta(
            ago(claim.observation.observedAt),
            claim.observation.nodeLabel,
          )}</span
        >
      </div>
    {/if}

    <div class="dag staged">
      {#each ranks as rank, rankIndex}
        {#if rankIndex}
          <div
            class:dim={edges[rankIndex - 1].dim}
            class:strong={edges[rankIndex - 1].strong}
            class="edge"
          ></div>
        {/if}
        <div class="rank">
          {#if rank.length > 1}
            <div class="stage-head">{rank.length} {P.labels.parallel}</div>
          {/if}
          {#each rank as node}
            <button
              type="button"
              class:attn={node.state === "escalated" || isHumanGate(node)}
              class:sel={node.key === selectedKey}
              class="node"
              aria-label={joinMeta(
                node.label,
                P.nodeStates[node.state] || node.state,
                nodeOwner(node),
                nodeTiming(node),
              )}
              aria-pressed={node.key === selectedKey}
              onclick={() => (selectedKey = node.key)}
            >
              <span class="nh">
                <StateIcon
                  icon={nodeIconKey(node)}
                  class={nodeStateClass(node)}
                />
                <span>{node.label}</span>
              </span>
              <span class="nm">
                <span>{nodeOwner(node)}</span>
                <span>{nodeTiming(node)}</span>
              </span>
              {#if node.kind !== "gate"}
                <span class="pips" aria-hidden="true">
                  {#each capacityPips(run.plan, node) as pip}
                    <span class={pip}></span>
                  {/each}
                </span>
              {/if}
            </button>
          {/each}
        </div>
      {/each}
    </div>

    {#if runView === "plan" && selectedNode}
      <section class:without-aside={!hasAside} class="detail-block">
        <div class="node-detail">
          <h2>
            {selectedNode.label}
            <span class="pill">{detailPill(selectedNode)}</span>
          </h2>
          {@render nodeDetail(selectedNode)}
        </div>
        {#if hasAside}
          <aside class="decide">
            {#if isHumanGate(selectedNode)}
              <p>{P.labels.decisionConsequence}</p>
              <!-- No run-detail decision endpoint exists yet; #4781 tracks the run as a mutable artifact. -->
              <button
                class="btn primary"
                type="button"
                aria-disabled="true"
                onclick={(event) => event.preventDefault()}
                >{P.labels.approve}</button
              >
              <button
                class="btn"
                type="button"
                aria-disabled="true"
                onclick={(event) => event.preventDefault()}
                >{P.labels.deny}</button
              >
              <p class="decision-note">{P.labels.decisionsInSession}</p>
            {:else}
              {#each selectedNode.attempts ?? [] as attempt}
                {@render sessionRow(selectedNode, attempt)}
              {/each}
            {/if}
          </aside>
        {/if}
      </section>
      {#if fallbackDeviations.length}
        <section class="run-deviations" data-register="fact">
          <h2>{P.labels.deviations}</h2>
          <div class="node-deviations">
            {#each fallbackDeviations as deviation}
              <div class="fact-deviation">
                <span class="dev-chip"
                  >{P.deviationCodes[deviation.code] ?? deviation.code}</span
                >
                <span>{deviation.text}</span>
              </div>
            {/each}
          </div>
        </section>
      {/if}
    {:else if runView === "log"}
      <section class="detail-block log-detail">
        <div class="run-log">
          {#each logEntries as entry}
            {@render logEntry(entry)}
          {/each}
        </div>
      </section>
    {/if}

    <section class="sessions">
      <h2>{P.labels.sessionsInRun}</h2>
      {#each attemptRows as row}
        {@render sessionRow(row.node, row.attempt)}
      {/each}
    </section>

    <div
      class:stale={view.engine_tier === "stale"}
      class="rv-foot"
      data-register="fact"
    >
      <span class="rv-id"
        >{P.labels.workflowWord} {shortId(run.workflow_id)}</span
      >
      {#if view.engine_tier !== "live"}
        <span>{engineStale(view.snapshot_age_seconds)}</span>
      {/if}
    </div>
  {/if}
</div>

{#snippet sessionRow(node, attempt)}
  <button
    class="srow"
    type="button"
    aria-disabled={!attempt.session_id}
    onclick={() => attempt.session_id && onSelectSession(attempt.session_id)}
  >
    <span
      class={`dot ${attempt.state === "running" ? "running" : attempt.state === "failed" ? "warn" : ""}`}
    ></span>
    <span>{attemptTitle(node, attempt)}</span>
    <span class="srow-meta">{attemptSummary(attempt)}</span>
  </button>
{/snippet}

{#snippet nodeDetail(node)}
  {#if node.blocked_on}
    <div class="log-entry" data-register="belief">{node.blocked_on.note}</div>
  {/if}
  {#if node.queue}
    <div class="log-entry" data-register="fact">
      {queuedOnQueue(node.queue.position, node.queue.name)}
    </div>
  {/if}
  {#if node.decision}
    {@render decision(node)}
  {/if}
  {#each node.attempts ?? [] as attempt}
    {@render attemptDetail(node, attempt)}
  {/each}
  {#if node.verdict}
    {@render verdict(node)}
  {/if}
  {#if node.evidence}
    {@render evidence(node)}
  {/if}
  {#if node.note}
    <div class="node-note">{node.note}</div>
  {/if}
  {#if deviationsForNode(node.key).length}
    <div class="node-deviations" data-register="fact">
      {#each deviationsForNode(node.key) as deviation}
        <div class="fact-deviation">
          <span class="dev-chip"
            >{P.deviationCodes[deviation.code] ?? deviation.code}</span
          >
          <span>{deviation.text}</span>
        </div>
      {/each}
    </div>
  {/if}
{/snippet}

{#snippet decision(node)}
  <div
    class="decision"
    data-register={node.decision.register}
    title={node.decision.basis}
  >
    <table>
      <tbody>
        {#each node.decision.outcomes as outcome}
          <tr>
            <td>{outcome.when}</td><td>{P.punct.arrow}</td><td
              >{outcome.then}</td
            >
          </tr>
        {/each}
      </tbody>
    </table>
  </div>
{/snippet}

{#snippet attemptDetail(node, attempt)}
  <div class="attempt-detail">
    {#if attempt.state === "running" && attempt.live?.activity}
      <div class="live-line">
        <span class="live-dot" aria-hidden="true"></span>
        <code class="act-cmd">{attempt.live.activity}</code>
        <span class="act-when">{ago(attempt.live.observed_at)}</span>
      </div>
    {/if}
    {#if attempt.finding}
      <div class="evidence" data-register="fact">
        <span class="ev-tag">{attempt.finding.code}</span>
        <span>{attempt.finding.text}</span>
        {#if attempt.finding.observed_head}
          <span class="sha">{attempt.finding.observed_head}</span>
        {/if}
      </div>
    {/if}
    {#if attempt.rationale?.parse_status === "parsed" || attempt.rationale?.parse_status === "unparseable"}
      <div class="testimony" data-register="testimony">
        <div class="testimony-attribution">
          {joinMeta(
            node.label,
            `${P.labels.attempt} ${attempt.n}`,
            attempt.model,
          )}
        </div>
        {#if attempt.rationale.parse_status === "parsed"}
          {#each attempt.rationale.paths as rationalePath}
            <div class="testimony-line">
              {joinMeta(rationalePath.path, rationalePath.why)}
            </div>
          {/each}
          {#each attempt.rationale.deviations as deviation}
            <div class="testimony-line">
              <span class="dev-chip">{P.labels.deviationWord}</span>
              <span>{deviation}</span>
            </div>
          {/each}
        {:else}
          <pre class="testimony-raw">{attempt.rationale.raw}</pre>
        {/if}
      </div>
    {/if}
  </div>
{/snippet}

{#snippet verdict(node)}
  <div
    class:wants-human={node.verdict.value !== "approve"}
    class="verdict-box"
    data-register="fact"
  >
    <div class="verdict-word">
      {P.labels.verdict}
      {P.stateWords[node.verdict.value] || node.verdict.value}
    </div>
    <div class="verdict-excerpt">{node.verdict.summary_plain}</div>
    {#if node.verdict.text_md}
      <pre class="verdict-text">{node.verdict.text_md}</pre>
    {/if}
    {#if node.verdict.commit_url}
      <a class="commit-link" href={node.verdict.commit_url}
        >{shortSha(node.verdict.commit_sha)}</a
      >
    {/if}
  </div>
{/snippet}

{#snippet evidence(node)}
  <div class="evidence" data-register="fact">
    <span class="ev-tag">{joinMeta(P.labels.evidence, node.evidence.kind)}</span
    >
    <span>{node.evidence.summary}</span>
  </div>
{/snippet}

{#snippet logEntry(entry)}
  <article class="flat-log-entry">
    <div class="flat-log-meta">
      <span>{entry.node.label}</span>
      <span>{ago(entry.at)}</span>
    </div>
    {#if entry.kind === "queue"}
      <div class="log-entry" data-register="fact">
        {queuedOnQueue(entry.node.queue.position, entry.node.queue.name)}
      </div>
    {:else if entry.kind === "blocked"}
      <div class="log-entry" data-register="belief">
        {entry.node.blocked_on.note}
      </div>
    {:else if entry.kind === "attempt"}
      <div class="log-entry" data-register="fact">
        {attemptTitle(entry.node, entry.attempt)}
      </div>
      {@render attemptDetail(entry.node, entry.attempt)}
    {:else if entry.kind === "decision"}
      {@render decision(entry.node)}
    {:else if entry.kind === "verdict"}
      {@render verdict(entry.node)}
    {:else if entry.kind === "evidence"}
      {@render evidence(entry.node)}
    {:else if entry.kind === "note"}
      <div class="node-note">{entry.node.note}</div>
    {:else if entry.kind === "deviation"}
      <div class="fact-deviation" data-register="fact">
        <span class="dev-chip"
          >{P.deviationCodes[entry.deviation.code] ??
            entry.deviation.code}</span
        >
        <span>{entry.deviation.text}</span>
      </div>
    {/if}
  </article>
{/snippet}
