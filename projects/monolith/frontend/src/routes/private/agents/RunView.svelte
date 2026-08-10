<script>
  import StateIcon from "./StateIcon.svelte";
  import {
    computeRanks,
    isWide,
    nodeIconKey,
    nodeStateClass,
    pipClass,
  } from "./dag.js";
  import { fmtCost, fmtDur, ordinal, relSeconds } from "./run-format.js";
  import { RUN_LEXICON as P } from "./run-lexicon.js";
  let {
    run,
    view,
    sessions = [],
    onSelectSession = () => {},
    onCancel = () => {},
  } = $props();
  const active = $derived(
    run?.dbos_status === "PENDING" || run?.dbos_status === "ENQUEUED",
  );
  const impl = $derived(run?.nodes?.find((node) => node.key === "implement"));
  const attempts = $derived(impl?.attempts?.length || 0);
  // Only for commit_sha, which the payload deliberately carries at full length
  // because commit_url needs it. Fields the contract already shortens (a
  // finding's observed_head) are rendered as given: re-truncating them here
  // would silently override the server, which owns every string this page
  // shows.
  const shortSha = (sha) => String(sha || "").slice(0, 8);
  const ago = (value) => fmtDur(relSeconds(value, view.now));
  const path = (i, suffix) => `run.nodes[${i}]${suffix}`;
</script>

<div class:tier-stale={view.engine_tier === "stale"} class="runview">
  {#if !run || view.engine_tier === "absent"}
    <div class="rv-eyebrow">
      <span class="eyebrow-label">{P.labels.run}</span><span class="state-chip"
        >{P.labels.sessionsOnly}</span
      >
    </div>
    <div class="absent-note">{P.labels.absentNotice}</div>
    <div class="sess-list">
      {#each sessions as session, i}
        <button
          class="sess-row"
          type="button"
          onclick={() => onSelectSession(session.id)}
        >
          <span
            class={`dot ${session.status === "running" ? "running" : session.status === "warn" ? "warn" : ""}`}
            aria-hidden="true"
          ></span>
          <span class="sess-title">{session.title}</span><span class="sess-meta"
            >{session.status}
            {P.punct.dot}
            {session.model}
            {fmtCost(session.total_cost_usd)}
            {P.punct.dot}
            {ago(session.last_turn_at)}
            {P.labels.ago}</span
          >
        </button>
      {/each}
    </div>
  {:else}
    <div class="rv-eyebrow">
      <span class={`state-chip s-${run.state}`}
        >{P.stateWords[run.state] || run.state}</span
      ><span class="rv-id">{run.workflow_id}</span>
    </div>
    <h2 class="rv-title">{run.task.text}</h2>
    <div class="rv-meta">
      {run.work_branch}
      {P.punct.dot}
      {P.labels.started}
      {ago(run.created_at)}
      {P.labels.ago}
      {#if fmtCost(run.cost_usd)}{P.punct.dot} {fmtCost(run.cost_usd)}{/if}
    </div>
    <div class="rv-statusline" data-register="fact">
      {#if run.state === "running" && attempts}
        <span class="fact-count"
          >{P.labels.attempt}
          {attempts}
          {P.labels.of}
          {run.plan?.max_attempts || attempts}</span
        >
        <span data-register="belief">
          {P.punct.dot} {P.labels.retriesRemain}</span
        >
      {:else if run.completed_at}
        {P.stateWords[run.state] || run.state}
        {#if run.cancelled_by}
          {P.labels.byWord} {run.cancelled_by.actor}{/if}
        {ago(run.completed_at)}
        {P.labels.ago}
      {:else if run.state === "queued"}
        {#if run.nodes.find((node) => node.queue)}{ordinal(
            run.nodes.find((node) => node.queue).queue.position,
          )}
          {P.labels.positionWord}{/if}
      {/if}
    </div>
    {#if run.stranded}<div class="banner">{P.labels.strandedBanner}</div>{/if}
    <div class="rv-actions">
      {#if active}<button
          class="btn-quiet btn-danger"
          type="button"
          disabled={view.engine_tier !== "live"}
          onclick={onCancel}>{P.labels.cancelRun}</button
        >{/if}
      {#if run.nodes.find((node) => node.verdict)?.verdict?.commit_url}<a
          class="link-out"
          href={run.nodes.find((node) => node.verdict).verdict.commit_url}
          >{P.labels.reviewedCommit}
          {shortSha(
            run.nodes.find((node) => node.verdict).verdict.commit_sha,
          )}</a
        >{/if}
    </div>
    <div class="staged">
      {#each computeRanks(run.nodes) as group, rank}
        {#if rank}<div class="connector"></div>{/if}
        {#if group.length > 1}<div class="stage">
            <div class="stage-head">{group.length} {P.labels.parallel}</div>
            <div class="lanes">
              {#each group as node}{@render nodeCard(node, true)}{/each}
            </div>
          </div>{:else}{@render nodeCard(group[0], false)}{/if}
      {/each}
    </div>
    <div
      class="rv-foot"
      class:stale={view.engine_tier === "stale"}
      data-register="fact"
    >
      {P.labels.engine}{P.punct.colon}
      {view.engine_tier === "live"
        ? P.labels.live
        : `${P.labels.staleShowing} ${fmtDur(view.snapshot_age_seconds)} ${P.labels.staleOld}`}
    </div>
  {/if}
</div>

{#snippet nodeCard(node, lane)}
  {@const i = run.nodes.indexOf(node)}
  {@const iconClass = nodeStateClass(node)}
  <div
    class={`${lane ? "lane-card" : "node-card"} ${node.state === "escalated" || node.blocked_on?.kind === "human" ? "trouble" : ""}`}
  >
    <div class={`card-head ${node.state === "running" ? "is-running" : ""}`}>
      <StateIcon icon={nodeIconKey(node)} class={iconClass} />
      <span class="card-label">{node.label}</span><span class="entry-meta"
        >{P.nodeStates[node.state] || node.state}</span
      >
      {#if node.attempts?.length}<span class="pips"
          >{#each node.attempts as attempt}<span class={pipClass(attempt)}
            ></span>{/each}</span
        >{/if}
      {#if node.state === "escalated" || node.blocked_on?.kind === "human"}<span
          class="needs-tag">{P.labels.needsYou}</span
        >{/if}
    </div>
    {#if node.blocked_on}<div class="log-entry" data-register="belief">
        {node.blocked_on.note}
      </div>{/if}
    {#if node.deps?.length > 1 || node.state === "blocked"}<div
        class="dep-chips"
      >
        <span class="entry-meta">{P.labels.waitsOn}</span
        >{#each node.deps as depKey}{@const dep = run.nodes.find(
            (n) => n.key === depKey,
          )}{#if dep}<span class="chip-dep"
              ><StateIcon
                icon={nodeIconKey(dep)}
                class={nodeStateClass(dep)}
              />{dep.label}</span
            >{/if}{/each}
      </div>{/if}
    {#each node.attempts ?? [] as attempt}
      <button
        class="log-entry"
        type="button"
        onclick={() =>
          attempt.session_id && onSelectSession(attempt.session_id)}
        ><span class="entry-meta"
          >{P.labels.attempt}
          {attempt.n}
          {P.punct.dot}
          {attempt.ended_at
            ? fmtDur(relSeconds(attempt.started_at, attempt.ended_at))
            : `${P.stateWords.running} ${fmtDur(relSeconds(attempt.started_at, view.now))}`}{#if fmtCost(attempt.cost_usd)}
            {P.punct.dot} {fmtCost(attempt.cost_usd)}{/if}</span
        >{#if attempt.finding}<span class="finding"
            >{attempt.finding.text} {attempt.finding.observed_head ?? ""}</span
          >{/if}</button
      >
    {/each}
    {#if node.verdict}<div class="verdict-box" data-register="belief">
        <div class="verdict-word">{P.labels.verdict} {node.verdict.value}</div>
        <div class="verdict-excerpt">{node.verdict.excerpt}</div>
        {#if node.verdict.commit_url}<a
            class="commit-link"
            href={node.verdict.commit_url}
            >{shortSha(node.verdict.commit_sha)}</a
          >{/if}
      </div>{/if}
    {#if node.evidence}<div class="log-entry">
        <span class="entry-meta"
          >{P.labels.evidence} {node.evidence.summary}</span
        >
      </div>{/if}
    {#if node.blocked_on?.kind === "human"}<div class="rv-actions">
        <button class="btn-quiet btn-approve" type="button"
          >{P.labels.approve}</button
        ><button class="btn-quiet" type="button">{P.labels.deny}</button>
      </div>{/if}
    {#if node.note}<div class="node-note">{node.note}</div>{/if}
  </div>
{/snippet}
