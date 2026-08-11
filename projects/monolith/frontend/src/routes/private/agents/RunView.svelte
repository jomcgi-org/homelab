<script>
  import StateIcon from "./StateIcon.svelte";
  import {
    computeRanks,
    isWide,
    nodeIconKey,
    nodeStateClass,
    capacityPips,
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

  // "running 2m" when the elapsed time is known, bare "running" when it is
  // not. Interpolating the duration directly would print the text "null" now
  // that formatters report absence honestly, which is a louder fabrication
  // than the "0s" it replaced. The phrase layer generalises this.
  const withDur = (word, seconds) => {
    const duration = fmtDur(seconds);
    return duration ? `${word} ${duration}` : word;
  };
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
      >
    </div>
    <h2 class="rv-title">{run.task.text}</h2>
    <div class="rv-meta">
      {run.work_branch}
      {P.punct.dot}
      {P.labels.started}
      {ago(run.created_at)}
      {P.labels.ago}
      {#if run.plan?.budget_usd != null}
        {P.punct.dot}
        {fmtCost(run.cost_usd)}
        {P.labels.of}
        {fmtCost(run.plan.budget_usd)}
        {P.labels.budgetWord}
      {:else if fmtCost(run.cost_usd)}{P.punct.dot} {fmtCost(run.cost_usd)}{/if}
    </div>
    <div class="rv-statusline" data-register="fact">
      {#if run.state === "running" && attempts && run.plan?.pinned}
        <span class="fact-count"
          >{P.labels.attempt}
          {attempts}
          {P.labels.of}
          {run.plan.max_attempts}</span
        >
      {:else if run.state === "running" && attempts}
        <span class="fact-count">{P.labels.attempt} {attempts}</span>
        <span data-register="belief"
          >{P.punct.dot} {P.labels.retriesRemain}</span
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
    {#if run.stranded}<div class="banner">
        {P.labels.strandedBanner}
        {P.labels.buildWord}
        {run.app_version}
        {P.punct.dot}
        {P.labels.serverBuildWord}
        {run.server_app_version}
      </div>{/if}
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
      {#if run.branch_url}<a class="link-out" href={run.branch_url}
          >{P.labels.openBranch}</a
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
    {#if run.deviations?.length}<div class="deviations" data-register="fact">
        <div class="stage-head">{P.labels.deviations}</div>
        {#each run.deviations as deviation}<div class="deviation-text mono">
            {deviation.text}
          </div>{/each}
      </div>{/if}
    <div
      class="rv-foot"
      class:stale={view.engine_tier === "stale"}
      data-register="fact"
    >
      <span class="rv-id">{run.workflow_id}</span>
      {#if view.engine_tier !== "live"}
        {P.punct.dot}
        {P.labels.engine}{P.punct.colon}
        {P.labels.staleShowing}
        {fmtDur(view.snapshot_age_seconds)}
        {P.labels.staleOld}
      {/if}
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
      {#if node.attempts?.length}<span class="pips">
          {#each capacityPips(run.plan, node) as pip}<span class={pip}
            ></span>{/each}
        </span>
        {#if run.plan?.pinned}<span class="entry-meta"
            >{run.plan.max_attempts} {P.labels.attempts}</span
          >{/if}
      {/if}
      {#if node.state === "escalated" || node.blocked_on?.kind === "human"}<span
          class="needs-tag">{P.labels.needsYou}</span
        >{/if}
    </div>
    {#if node.blocked_on}<div class="log-entry" data-register="belief">
        {node.blocked_on.note}
      </div>{/if}
    {#if node.model}<div class="entry-meta">{node.model}</div>{/if}
    {#if node.queue}<div class="log-entry" data-register="fact">
        {ordinal(node.queue.position)}
        {P.labels.queuedOn}
        {node.queue.name}
        {P.labels.queueWord}
      </div>{/if}
    {#if node.decision}<div
        class="decision"
        data-register={node.decision.register}
        title={node.decision.basis}
      >
        <div class="entry-meta">
          {P.labels.gateWord}
          {P.labels.whenTurnEnds}
        </div>
        <table>
          <tbody
            >{#each node.decision.outcomes as outcome}<tr
                ><td>{outcome.when}</td><td>{P.punct.arrow}</td><td
                  >{outcome.then}</td
                ></tr
              >{/each}</tbody
          >
        </table>
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
      <div>
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
              : withDur(
                  P.stateWords.running,
                  relSeconds(attempt.started_at, view.now),
                )}{#if fmtCost(attempt.cost_usd)}
              {P.punct.dot} {fmtCost(attempt.cost_usd)}{/if}</span
          >{#if attempt.state === "running" && attempt.live?.activity}<span
              class="live-line"
              ><span class="live-dot"></span><span class="live-act"
                >{attempt.live.activity}</span
              >{ago(attempt.live.observed_at)}
              {P.labels.ago}</span
            >{/if}{#if attempt.finding}<span class="finding"
              >{attempt.finding.text}
              {attempt.finding.observed_head ?? ""}</span
            >{/if}</button
        >
        {#if attempt.rationale?.parse_status === "parsed" || attempt.rationale?.parse_status === "unparseable"}
          <div class="testimony" data-register="testimony">
            <div class="testimony-attribution">
              {node.label}
              {P.punct.dot}
              {P.labels.attempt}
              {attempt.n}{#if attempt.model}
                {P.punct.dot} {attempt.model}{/if}
            </div>
            {#if attempt.rationale.parse_status === "parsed"}
              {#each attempt.rationale.areas as area}
                <div class="testimony-line">
                  {area.area}{#if area.why}
                    {P.punct.dot} {area.why}{/if}
                </div>
              {/each}
              {#each attempt.rationale.deviations as deviation}
                <div class="testimony-line">
                  {P.labels.deviationWord}
                  {deviation}
                </div>
              {/each}
            {:else}
              <pre class="testimony-raw">{attempt.rationale.raw}</pre>
            {/if}
          </div>
        {/if}
      </div>
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
