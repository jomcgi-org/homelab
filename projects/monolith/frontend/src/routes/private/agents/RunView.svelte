<script>
  import StateIcon from "./StateIcon.svelte";
  import {
    computeRanks,
    isWide,
    nodeIconKey,
    nodeStateClass,
    capacityPips,
  } from "./dag.js";
  import {
    agoPhrase,
    attemptMeta,
    engineStale,
    fmtCost,
    joinMeta,
    queuePosition,
    queuedOnQueue,
    relSeconds,
    spendOfBudget,
    startedAgo,
    firstLine,
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
  const since = (value) => relSeconds(value, view.now);
  const ago = (value) => agoPhrase(since(value));
  // Ids are plumbing: needed for copy and correlation, never for scanning.
  const shortId = (id) => String(id || "").slice(-12);
  const path = (i, suffix) => `run.nodes[${i}]${suffix}`;
  let copied = $state(false);

  function copyWorkflowId() {
    navigator.clipboard?.writeText(run.workflow_id);
    copied = true;
    setTimeout(() => (copied = false), 1200);
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
    <PaneHeader
      kind={P.labels.run}
      crumbs={crumbTrail({ kind: "run", runTitle: firstLine(run.task.text) })}
      {onCrumb}
    >
      {#snippet chips()}
        <span
          class={`state-chip s-${run.state}`}
          class:unconfirmed={claim.unconfirmed}
          >{P.stateWords[run.state] || run.state}</span
        >
      {/snippet}
    </PaneHeader>
    <h2 class="rv-title" title={run.task.text}>{firstLine(run.task.text)}</h2>
    <div class="rv-meta">
      {joinMeta(
        run.work_branch,
        startedAgo(since(run.created_at)),
        spendOfBudget(run.cost_usd, run.plan?.budget_usd),
      )}
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
        {joinMeta(
          run.cancelled_by
            ? `${P.stateWords[run.state] || run.state} ${P.labels.byWord} ${run.cancelled_by.actor}`
            : P.stateWords[run.state] || run.state,
          ago(run.completed_at),
        )}
      {:else if run.state === "queued"}
        {#if run.nodes.find((node) => node.queue)}{queuePosition(
            run.nodes.find((node) => node.queue).queue.position,
          )}{/if}
      {/if}
    </div>
    <!-- String(), because the old markup rendered task.text bare and an absent
         one merely looked wrong. Calling .includes on it turns the same absent
         value into a TypeError that blanks the entire pane, which is a worse
         failure than the one being fixed. -->
    {#if String(run.task.text ?? "").includes("\n")}
      <details class="rv-task-details">
        <summary>{P.labels.fullTask}</summary>
        <pre>{run.task.text}</pre>
      </details>
    {/if}
    {#if run.stranded}<div class="banner" data-register="belief">
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
      </div>{/if}
    {#if claim.terminal && claim.observation}<div
        class="obs-line"
        data-register="fact"
      >
        <span class="lbl">{P.labels.lastActivity}</span>
        <span
          >{joinMeta(
            ago(claim.observation.observedAt),
            claim.observation.nodeLabel,
          )}</span
        >
      </div>{/if}
    {#if claim.unconfirmed}<div class="conflict" data-register="belief">
        {P.labels.stateConflict}
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
        {#each run.deviations as deviation}<div class="deviation-text">
            <span class="dev-chip"
              >{P.deviationCodes[deviation.code] ?? deviation.code}</span
            >
            <span>{deviation.text}</span>
          </div>{/each}
      </div>{/if}
    <div
      class="rv-foot"
      class:stale={view.engine_tier === "stale"}
      data-register="fact"
    >
      <span class="rv-id"
        >{P.labels.workflowWord}
        {shortId(run.workflow_id)}</span
      ><button
        class="copy-id"
        type="button"
        title={run.workflow_id}
        onclick={copyWorkflowId}
        >{copied ? P.labels.copied : P.labels.copyId}</button
      >
      {#if view.engine_tier !== "live"}
        <span>{engineStale(view.snapshot_age_seconds)}</span>
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
        {queuedOnQueue(node.queue.position, node.queue.name)}
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
      {@const attemptLine = attempt.ended_at
        ? attemptMeta(
            attempt.n,
            null,
            relSeconds(attempt.started_at, attempt.ended_at),
            attempt.cost_usd,
          )
        : attemptMeta(
            attempt.n,
            P.stateWords.running,
            since(attempt.started_at),
            attempt.cost_usd,
          )}
      <div>
        <!-- The accessible name carries the attempt line as well as the
             affordance. A bare aria-label of "open session" would replace the
             name rather than add to it, so a screen reader would hear which
             control this is but never which attempt it opens. -->
        <button
          class="log-entry"
          type="button"
          aria-label={joinMeta(attemptLine, P.labels.openSession)}
          onclick={() =>
            attempt.session_id && onSelectSession(attempt.session_id)}
          ><span class="entry-meta">{attemptLine}</span></button
        >
        {#if attempt.state === "running" && attempt.live?.activity}<div
            class="live-line"
          >
            <span class="live-dot" aria-hidden="true"></span>
            <code class="act-cmd">{attempt.live.activity}</code>
            <span class="act-when">{ago(attempt.live.observed_at)}</span>
          </div>{/if}
        {#if attempt.finding}<div class="evidence" data-register="fact">
            <span class="ev-tag">{attempt.finding.code}</span>
            <span>{attempt.finding.text}</span>
            {#if attempt.finding.observed_head}<span class="sha"
                >{attempt.finding.observed_head}</span
              >{/if}
          </div>{/if}
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
              {#each attempt.rationale.areas as area}
                <div class="testimony-line">
                  {joinMeta(area.area, area.why)}
                </div>
              {/each}
              {#each attempt.rationale.deviations as deviation}
                <div class="testimony-line">
                  <!-- The label is a chip, not the sentence's first word.
                       Its gap is CSS, so no template whitespace can be
                       trimmed away and glue it to the text. -->
                  <span class="dev-chip">{P.labels.deviationWord}</span><span
                    >{deviation}</span
                  >
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
    {#if node.evidence}<div class="evidence" data-register="fact">
        <span class="ev-tag"
          >{joinMeta(P.labels.evidence, node.evidence.kind)}</span
        >
        <span>{node.evidence.summary}</span>
      </div>{/if}
    {#if node.blocked_on?.kind === "human"}<div class="rv-actions">
        <button class="btn-quiet btn-approve" type="button"
          >{P.labels.approve}</button
        ><button class="btn-quiet" type="button">{P.labels.deny}</button>
      </div>{/if}
    {#if node.note}<div class="node-note">{node.note}</div>{/if}
  </div>
{/snippet}
