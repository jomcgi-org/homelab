<script>
  import { onMount } from "svelte";
  import { periodForHour } from "$lib/private/period.js";
  import "../agents-theme.css";
  import { renderAgentMarkdown } from "../markdown.js";
  import { fmtCost } from "../run-format.js";
  import { relativeTime } from "../run-history.js";
  import { DRAIN_LEXICON as D } from "./lexicon.js";
  import {
    activityLine,
    ageSeconds,
    clockOffsetMs,
    filterJobs,
    fingerprintFraction,
    fmtClockAge,
    fmtDuration,
    isRunaway,
    jobCalls,
    jobClass,
    laneClass,
    linkifyRefs,
  } from "./console-model.js";

  const POLL_MS = 5000;

  let data = $state(null);
  let loadError = $state(null);
  let actionError = $state(null);
  let offsetMs = $state(0);
  let nowMs = $state(Date.now());
  let filter = $state("all");
  let expanded = $state(null);
  let detail = $state(null);
  let detailLoading = $state(false);
  let kicking = $state(false);
  let cancelling = $state(false);
  let requeueing = $state(null);

  const lane = $derived(data?.lane ?? null);
  const cycle = $derived(lane?.cycle ?? null);
  const laneWord = $derived(D.laneWords[lane?.state] || lane?.state || "");
  const laneHint = $derived(D.laneHints[lane?.state] || "");
  const checkpointAge = $derived(
    cycle ? ageSeconds(cycle.last_checkpoint_at, offsetMs, nowMs) : null,
  );
  const cycleAge = $derived(
    cycle ? ageSeconds(cycle.created_at, offsetMs, nowMs) : null,
  );
  const visibleJobs = $derived(filterJobs(data?.jobs, filter));
  const filterStates = $derived.by(() => {
    const counts = data?.queue || {};
    return ["running", "due", "scheduled", "error", "ok", "parked"].filter(
      (state) => (counts[state] || 0) > 0,
    );
  });
  const cancellable = $derived(
    Boolean(cycle) && ["PENDING", "ENQUEUED"].includes(cycle?.status),
  );

  async function load() {
    try {
      const response = await fetch("/agents/drain/console");
      if (!response.ok) throw new Error(`backend ${response.status}`);
      const body = await response.json();
      offsetMs = clockOffsetMs(body.now);
      data = body;
      loadError = null;
    } catch {
      // Keep the last known frame; say the feed is broken instead of
      // repainting a confident empty state.
      loadError = D.labels.unavailable;
    }
  }

  async function loadDetail(name) {
    detailLoading = true;
    detail = null;
    try {
      const response = await fetch(
        `/agents/drain/jobs/${encodeURIComponent(name)}`,
      );
      if (!response.ok) throw new Error(`backend ${response.status}`);
      detail = await response.json();
    } catch {
      detail = { error: D.labels.detailError };
    } finally {
      detailLoading = false;
    }
  }

  function toggle(name) {
    if (expanded === name) {
      expanded = null;
      detail = null;
      return;
    }
    expanded = name;
    loadDetail(name);
  }

  function stopPropagation(event) {
    event.stopPropagation();
  }

  async function kick() {
    if (kicking) return;
    kicking = true;
    actionError = null;
    try {
      const response = await fetch("/agents/drain/kick", { method: "POST" });
      if (!response.ok) throw new Error(`backend ${response.status}`);
      await load();
    } catch {
      actionError = D.labels.kickFailed;
    } finally {
      kicking = false;
    }
  }

  async function cancelCycle() {
    const id = cycle?.workflow_id;
    if (!id || cancelling) return;
    if (!window.confirm(D.labels.cancelConfirm)) return;
    cancelling = true;
    actionError = null;
    try {
      const response = await fetch(
        `/agents/runs/${encodeURIComponent(id)}/cancel`,
        { method: "POST" },
      );
      if (!response.ok) throw new Error(`backend ${response.status}`);
      await load();
    } catch {
      actionError = D.labels.cancelFailed;
    } finally {
      cancelling = false;
    }
  }

  async function requeue(name) {
    if (requeueing) return;
    requeueing = name;
    actionError = null;
    try {
      const response = await fetch(
        `/agents/drain/jobs/${encodeURIComponent(name)}/requeue`,
        { method: "POST" },
      );
      if (response.status === 409) {
        actionError = D.labels.requeueRunning;
        return;
      }
      if (!response.ok) throw new Error(`backend ${response.status}`);
      await Promise.all([load(), expanded === name ? loadDetail(name) : null]);
    } catch {
      actionError = D.labels.requeueFailed;
    } finally {
      requeueing = null;
    }
  }

  function shortId(value) {
    return String(value || "").slice(0, 8);
  }

  function fill(template, values) {
    return Object.entries(values).reduce(
      (text, [key, value]) => text.replaceAll(`{${key}}`, String(value)),
      template,
    );
  }

  function prState(pr) {
    if (pr?.merged) return "merged";
    return ["open", "closed"].includes(pr?.state) ? pr.state : null;
  }

  function prStateLabel(pr) {
    return {
      open: D.labels.prStateOpen,
      closed: D.labels.prStateClosed,
      merged: D.labels.prStateMerged,
    }[prState(pr)];
  }

  $effect(() => {
    if (typeof document !== "undefined") {
      document.documentElement.setAttribute(
        "data-agents-period",
        periodForHour(new Date(nowMs).getHours()),
      );
    }
  });

  onMount(() => {
    load();
    const poll = setInterval(load, POLL_MS);
    // A one-second tick drives the checkpoint clock; watching that number
    // move (or freeze) is the wedge indicator working.
    const clock = setInterval(() => (nowMs = Date.now()), 1000);
    return () => {
      clearInterval(poll);
      clearInterval(clock);
    };
  });
</script>

<svelte:head>
  <title>{D.labels.title}</title>
</svelte:head>

<div class="console drain-page">
  <div class="wrap">
    <header class="top">
      <a class="back mono" href="/agents">&larr; {D.labels.backToAgents}</a>
      <h1>{D.labels.title}</h1>
      <span class="grow" aria-hidden="true"></span>
      <button
        class="ghost mono"
        type="button"
        disabled={kicking}
        onclick={kick}
      >
        {kicking ? D.labels.kicking : D.labels.kick}
      </button>
    </header>

    {#if actionError}
      <p class="action-error" role="alert">{actionError}</p>
    {/if}

    {#if !data}
      <p class="quiet-note mono">{loadError || D.labels.loading}</p>
    {:else}
      <section
        class={`rail state-${laneClass(lane?.state)}`}
        role="status"
        aria-label={`${D.labels.title}: ${laneWord}`}
      >
        <div class="rail-head">
          <span class={`dot ${laneClass(lane?.state)}`} aria-hidden="true"
          ></span>
          <span class="word">{laneWord}</span>
          <span class="hint">{laneHint}</span>
          {#if loadError}
            <span class="stale mono">{loadError}</span>
          {/if}
        </div>
        {#if cycle}
          <div class="rail-meta mono">
            <span class="clock">
              {fmtClockAge(checkpointAge)}
            </span>
            <span>{D.labels.lastCheckpoint}</span>
            <span class="sep" aria-hidden="true">{D.labels.dot}</span>
            <span>
              {D.labels.cycleWord}
              {shortId(cycle.workflow_id)}
              {cycle.status}
              {fmtClockAge(cycleAge)}
            </span>
            {#if cycle.last_step}
              <span class="sep" aria-hidden="true">{D.labels.dot}</span>
              <span>{D.labels.lastStep} {cycle.last_step}</span>
            {/if}
            <span class="sep" aria-hidden="true">{D.labels.dot}</span>
            <span>
              {fill(D.labels.claimsOfFinishes, {
                finishes: cycle.finishes,
                claims: cycle.claims,
              })}
            </span>
            {#if data.queue?.due}
              <span class="sep" aria-hidden="true">{D.labels.dot}</span>
              <span>
                {fill(D.labels.queuedBehind, { count: data.queue.due })}
              </span>
            {/if}
          </div>
          <div class="rail-actions">
            <span class="reap mono">
              {fill(D.labels.reapNote, {
                duration: fmtDuration(lane.reap_after_seconds),
              })}
            </span>
            {#if cancellable}
              <button
                class="danger mono"
                type="button"
                disabled={cancelling}
                onclick={cancelCycle}
              >
                {cancelling ? D.labels.cancelling : D.labels.cancelCycle}
              </button>
            {/if}
          </div>
        {:else if lane?.error}
          <div class="rail-meta mono">{lane.error}</div>
        {/if}
      </section>

      {#if data.recent_cycles?.length}
        <section class="cycles" aria-label={D.labels.recentCycles}>
          <span class="cycles-label mono">{D.labels.recentCycles}</span>
          {#each data.recent_cycles as recent (recent.workflow_id)}
            <span
              class={`cycle-chip mono ${recent.status === "SUCCESS" ? "" : "bad"}`}
              title={`${recent.workflow_id} ${recent.status}`}
            >
              {recent.status === "SUCCESS" ? "ok" : recent.status.toLowerCase()}
              {#if recent.duration_seconds != null}
                {fmtDuration(recent.duration_seconds)}
              {/if}
              {recent.finishes}j
              {relativeTime(recent.created_at)}
            </span>
          {/each}
        </section>
      {/if}

      <section class="jobs" aria-label={D.labels.jobsHeading}>
        <div class="filters" role="group" aria-label={D.labels.jobsHeading}>
          <button
            class="chip mono"
            class:active={filter === "all"}
            type="button"
            aria-pressed={filter === "all"}
            onclick={() => (filter = "all")}
          >
            {D.labels.filterAll}
            {data.jobs?.length ?? 0}
          </button>
          {#each filterStates as state (state)}
            <button
              class={`chip mono chip-${jobClass(state)}`}
              class:active={filter === state}
              type="button"
              aria-pressed={filter === state}
              onclick={() => (filter = filter === state ? "all" : state)}
            >
              {D.jobStates[state]}
              {data.queue?.[state] ?? 0}
            </button>
          {/each}
        </div>

        <div class="job-list">
          {#each visibleJobs as job (job.name)}
            {@const calls = jobCalls(job)}
            <!-- The nested button provides keyboard access while this div enlarges the pointer target. -->
            <!-- svelte-ignore a11y_no_static_element_interactions a11y_click_events_have_key_events -->
            <div
              class="job-row"
              class:open={expanded === job.name}
              onclick={() => toggle(job.name)}
            >
              <button
                class="job-toggle"
                type="button"
                aria-expanded={expanded === job.name}
                onclick={(event) => {
                  stopPropagation(event);
                  toggle(job.name);
                }}
              >
                <span
                  class={`dot ${jobClass(job.state)}`}
                  title={D.jobStates[job.state] || job.state}
                ></span>
                <span class="job-main">
                  <span class="job-name mono">{job.name}</span>
                  <span class="job-sub">
                    {#if job.state === "error" && job.summary_head}
                      <span class="err-text">{job.summary_head}</span>
                    {:else if job.state === "ok" && job.outcome === "pr" && job.pr}
                      <span class="pr-space" aria-hidden="true"></span>
                    {:else if job.state === "ok"}
                      {job.summary_head || job.prompt_head || D.labels.dash}
                    {:else}
                      {job.prompt_head || job.summary_head || D.labels.dash}
                    {/if}
                  </span>
                </span>
              </button>
              {#if job.state === "ok" && job.outcome === "pr" && job.pr}
                <a
                  class="pr-ref mono"
                  href={job.pr.url}
                  target="_blank"
                  rel="noreferrer"
                  onclick={stopPropagation}
                  >{D.labels.numberMark}{job.pr.number}</a
                >
              {/if}
              <span class="job-calls mono" class:runaway={isRunaway(calls)}>
                {#if calls != null}
                  <span class="track" aria-hidden="true">
                    <span
                      class="bar"
                      style={`width:${Math.round(fingerprintFraction(calls) * 100)}%`}
                    ></span>
                  </span>
                  {calls}
                  {job.state === "running"
                    ? D.labels.liveCallsWord
                    : D.labels.callsWord}
                {/if}
              </span>
              <span class="job-meta mono">
                {#if job.session?.cost_usd}
                  {fmtCost(job.session.cost_usd)}
                {/if}
              </span>
              <span class="job-age mono">
                {relativeTime(
                  job.state === "running"
                    ? job.locked_at
                    : job.last_run_at || job.next_run_at,
                )}
              </span>
              <span class={`job-state mono state-${jobClass(job.state)}`}>
                {D.jobStates[job.state] || job.state}
              </span>
            </div>

            {#if expanded === job.name}
              <div class="job-detail">
                {#if detailLoading}
                  <p class="quiet-note mono">{D.labels.loading}</p>
                {:else if detail?.error}
                  <p class="quiet-note mono">{detail.error}</p>
                {:else if detail}
                  <div class="detail-head">
                    {#if detail.job.repo}
                      <a
                        class="mono detail-kind detail-repo"
                        href={`https://github.com/${detail.job.repo}${detail.job.branch ? `/tree/${detail.job.branch}` : ""}`}
                        target="_blank"
                        rel="noreferrer"
                      >
                        {detail.job.repo}{detail.job.branch
                          ? `@${detail.job.branch}`
                          : ""}
                      </a>
                    {:else}
                      <span class="mono detail-kind">
                        {detail.job.repo || ""}{detail.job.branch
                          ? `@${detail.job.branch}`
                          : ""}
                      </span>
                    {/if}
                    <span class="grow" aria-hidden="true"></span>
                    {#if ["error", "parked", "ok"].includes(detail.job.state)}
                      <button
                        class="ghost mono"
                        type="button"
                        disabled={requeueing === job.name}
                        onclick={() => requeue(job.name)}
                      >
                        {requeueing === job.name
                          ? D.labels.requeueing
                          : D.labels.requeue}
                      </button>
                    {/if}
                  </div>
                  <h3 class="detail-label">{D.labels.promptWord}</h3>
                  <pre class="detail-pre">{detail.job.prompt ||
                      D.labels.dash}</pre>
                  {#if detail.job.outcome === "pr" && detail.job.pr}
                    {@const state = prState(detail.job.pr)}
                    <h3 class="detail-label">{D.labels.prCard}</h3>
                    <div class="pr-card">
                      <div class="pr-card-head">
                        <a
                          class="pr-number mono"
                          href={detail.job.pr.url}
                          target="_blank"
                          rel="noreferrer"
                          aria-label={`${D.labels.openPr} ${D.labels.numberMark}${detail.job.pr.number}`}
                        >
                          {D.labels.numberMark}{detail.job.pr.number}
                        </a>
                        {#if state}
                          <span class={`pr-state mono pr-state-${state}`}>
                            {prStateLabel(detail.job.pr)}
                          </span>
                        {/if}
                      </div>
                      {#if detail.job.pr.title}
                        <div class="pr-title">{detail.job.pr.title}</div>
                      {/if}
                      {#if detail.job.pr.changed_files != null && detail.job.pr.additions != null && detail.job.pr.deletions != null}
                        <div class="pr-stats mono">
                          <span>
                            {detail.job.pr.changed_files}
                            {detail.job.pr.changed_files === 1
                              ? D.labels.fileWord
                              : D.labels.filesWord}
                          </span>
                          <span class="pr-add">
                            {D.labels.additionMark}{detail.job.pr.additions}
                          </span>
                          <span class="pr-del">
                            {D.labels.deletionMark}{detail.job.pr.deletions}
                          </span>
                        </div>
                      {/if}
                    </div>
                  {/if}
                  {#if detail.job.last_summary}
                    {#if detail.job.outcome === "pr"}
                      {@const summaryWithoutUrl = detail.job.last_summary
                        .replace(
                          /https:\/\/github\.com\/[^\s]+\/pull\/\d+/g,
                          "",
                        )
                        .trim()}
                      {#if summaryWithoutUrl}
                        <h3 class="detail-label">
                          {D.labels.resultWordForReport}
                        </h3>
                        <div class="result-md">
                          {@html renderAgentMarkdown(
                            linkifyRefs(summaryWithoutUrl, detail.job.repo),
                          )}
                        </div>
                      {/if}
                    {:else if detail.job.outcome === "report"}
                      <h3 class="detail-label">
                        {D.labels.resultWordForReport}
                      </h3>
                      <div class="result-md">
                        {@html renderAgentMarkdown(
                          linkifyRefs(detail.job.last_summary, detail.job.repo),
                        )}
                      </div>
                    {:else}
                      <h3 class="detail-label">{D.labels.resultWord}</h3>
                      <pre class="detail-pre">{detail.job.last_summary}</pre>
                    {/if}
                  {/if}
                  {#each detail.attempts as attempt (attempt.session_id)}
                    <div class="attempt">
                      <div class="attempt-head mono">
                        <span>
                          {detail.attempts.length > 1
                            ? `${D.labels.attemptWord} ${detail.attempts.length - detail.attempts.indexOf(attempt)}`
                            : D.labels.attemptWord}
                        </span>
                        <span class="sep" aria-hidden="true"
                          >{D.labels.dot}</span
                        >
                        <span>{attempt.status}</span>
                        {#if attempt.turn?.terminal_reason}
                          <span class="sep" aria-hidden="true"
                            >{D.labels.dot}</span
                          >
                          <span>{attempt.turn.terminal_reason}</span>
                        {/if}
                        <span class="sep" aria-hidden="true"
                          >{D.labels.dot}</span
                        >
                        <span>{relativeTime(attempt.created_at)}</span>
                        <span class="grow" aria-hidden="true"></span>
                        <a
                          class="session-link mono"
                          href={`/agents?session=${attempt.session_id}`}
                        >
                          {D.labels.openSession} &rarr;
                        </a>
                      </div>
                      {#if attempt.live?.calls != null}
                        <p class="quiet-note mono">
                          {attempt.live.calls}
                          {D.labels.liveCallsWord}
                        </p>
                      {/if}
                      {#if attempt.activities?.length}
                        <p class="quiet-note mono">
                          {fill(D.labels.activityTailNote, {
                            shown: attempt.activities.length,
                            total: attempt.calls,
                          })}
                        </p>
                        <ol class="activities mono">
                          {#each attempt.activities as activity, index (index)}
                            <li>{activityLine(activity)}</li>
                          {/each}
                        </ol>
                      {:else}
                        <p class="quiet-note mono">{D.labels.noActivity}</p>
                      {/if}
                    </div>
                  {/each}
                {/if}
              </div>
            {/if}
          {:else}
            <p class="quiet-note mono">
              {data.jobs?.length ? D.labels.noneMatching : D.labels.noJobs}
            </p>
          {/each}
        </div>
      </section>
    {/if}
  </div>
</div>

<style>
  .drain-page {
    min-height: 100dvh;
    background: var(--page-bg);
    color: var(--text);
    font-family: var(--font-ui);
    font-size: var(--size-body);
    line-height: 1.45;
  }
  .wrap {
    max-width: 960px;
    margin: 0 auto;
    padding: 24px 16px 64px;
    display: flex;
    flex-direction: column;
    gap: 16px;
  }
  .mono {
    font-family: var(--font-mono);
  }
  .grow {
    flex: 1;
  }
  .top {
    display: flex;
    align-items: baseline;
    gap: 14px;
  }
  .top h1 {
    margin: 0;
    font-size: 20px;
    font-weight: 600;
    letter-spacing: -0.01em;
  }
  .back {
    color: var(--muted);
    font-size: 12px;
    text-decoration: none;
  }
  .back:hover {
    color: var(--text);
  }
  .ghost {
    padding: 5px 10px;
    border: 1px solid var(--line-strong);
    border-radius: var(--radius-md);
    color: var(--text-soft);
    background: var(--panel-bg);
    font-size: 12px;
  }
  .ghost:hover:not(:disabled) {
    background: var(--hover);
  }
  .ghost:disabled,
  .danger:disabled {
    cursor: not-allowed;
    opacity: 0.5;
  }
  .danger {
    padding: 5px 10px;
    border: 1px solid var(--err);
    border-radius: var(--radius-md);
    color: var(--err);
    background: var(--panel-bg);
    font-size: 12px;
  }
  .danger:hover:not(:disabled) {
    background: var(--err-bg);
  }
  button:focus-visible,
  a:focus-visible {
    outline: 2px solid var(--info);
    outline-offset: 2px;
  }
  .action-error {
    margin: 0;
    padding: 8px 12px;
    border: 1px solid var(--err-line);
    border-radius: var(--radius-md);
    color: var(--err);
    background: var(--err-bg);
    font-size: 13px;
  }

  /* The health rail. One strip, one state word, and the checkpoint clock
     large enough that a frozen number is visible from across the room.
     State is carried by the left border and the dot rather than a full
     tinted panel, so the wedged red still lands without shouting on the
     healthy path. */
  .rail {
    padding: 12px 14px;
    border: 1px solid var(--line);
    border-left: 3px solid var(--dot-idle);
    border-radius: var(--radius-lg);
    background: var(--panel-bg);
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .rail.state-ok {
    border-left-color: var(--ok);
  }
  .rail.state-attn {
    border-left-color: var(--attn);
  }
  .rail.state-err {
    border-left-color: var(--err);
    background: var(--err-bg);
  }
  .rail-head {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
  }
  .word {
    font-weight: 600;
  }
  .rail.state-err .word {
    color: var(--err);
  }
  .hint {
    overflow: hidden;
    color: var(--muted);
    font-size: var(--size-detail);
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .stale {
    margin-left: auto;
    color: var(--attn-text);
    font-size: var(--size-meta);
    white-space: nowrap;
  }
  .rail-meta {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 6px;
    color: var(--text-soft);
    font-size: var(--size-body-mono);
  }
  .clock {
    font-size: 18px;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
    color: var(--text);
  }
  .rail.state-err .clock {
    color: var(--err);
  }
  .sep {
    color: var(--muted);
  }
  .rail-actions {
    display: flex;
    align-items: center;
    gap: 12px;
  }
  .reap {
    color: var(--muted);
    font-size: var(--size-meta);
  }
  .rail-actions .danger {
    margin-left: auto;
  }

  .cycles {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;
  }
  .cycles-label {
    color: var(--muted);
    font-size: var(--size-meta);
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }
  .cycle-chip {
    padding: 2px 8px;
    border: 1px solid var(--line);
    border-radius: var(--radius-pill);
    color: var(--text-soft);
    background: var(--panel-bg);
    font-size: var(--size-meta);
    white-space: nowrap;
  }
  .cycle-chip.bad {
    border-color: var(--err-line);
    color: var(--err);
  }

  .filters {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    padding-bottom: 8px;
  }
  .chip {
    padding: 4px 10px;
    border: 1px solid var(--line);
    border-radius: var(--radius-pill);
    color: var(--text-soft);
    background: var(--panel-bg);
    font-size: 12px;
  }
  .chip:hover {
    background: var(--hover);
  }
  .chip.active {
    border-color: var(--ink);
    color: var(--ink-text);
    background: var(--ink);
  }
  .chip-err:not(.active) {
    color: var(--err);
  }

  .job-list {
    display: flex;
    flex-direction: column;
    border: 1px solid var(--line);
    border-radius: var(--radius-lg);
    background: var(--panel-bg);
    overflow: hidden;
  }
  .job-row {
    position: relative;
    width: 100%;
    min-height: 46px;
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 8px 12px;
    border: 0;
    border-bottom: 1px solid var(--line);
    color: inherit;
    background: transparent;
    font-size: var(--size-body);
    text-align: left;
  }
  .job-toggle {
    min-width: 0;
    align-self: stretch;
    display: flex;
    flex: 1;
    align-items: center;
    gap: 12px;
    padding: 0;
    border: 0;
    color: inherit;
    background: transparent;
    font: inherit;
    text-align: left;
  }
  .job-list > :last-child {
    border-bottom: 0;
  }
  .job-row:hover,
  .job-row.open {
    background: var(--hover);
  }
  .dot {
    width: 8px;
    height: 8px;
    flex: 0 0 8px;
    border-radius: var(--radius-circle);
    background: var(--dot-idle);
  }
  .dot.ok {
    background: var(--ok);
  }
  .dot.attn {
    background: var(--attn);
  }
  .dot.err {
    background: var(--err);
  }
  @media (prefers-reduced-motion: no-preference) {
    .rail .dot.ok {
      animation: pulse 2s ease-in-out infinite;
    }
    @keyframes pulse {
      50% {
        opacity: 0.35;
      }
    }
  }
  .job-main {
    min-width: 0;
    flex: 1;
    display: grid;
    gap: 2px;
  }
  .job-name {
    overflow: hidden;
    font-size: 13px;
    font-weight: 500;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .job-sub {
    overflow: hidden;
    color: var(--text-soft);
    font-size: 12px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .err-text {
    color: var(--err);
  }
  .job-calls {
    display: flex;
    align-items: center;
    gap: 6px;
    flex: 0 0 140px;
    color: var(--text-soft);
    font-size: var(--size-meta);
    white-space: nowrap;
  }
  .job-calls.runaway {
    color: var(--err);
  }
  .track {
    width: 48px;
    height: 4px;
    border-radius: 2px;
    background: var(--line);
    overflow: hidden;
  }
  .bar {
    display: block;
    height: 100%;
    background: var(--dot-idle);
  }
  .runaway .bar {
    background: var(--err);
  }
  .job-meta {
    flex: 0 0 52px;
    color: var(--muted);
    font-size: var(--size-meta);
    text-align: right;
  }
  .job-age {
    flex: 0 0 44px;
    color: var(--muted);
    font-size: var(--size-meta);
    text-align: right;
    white-space: nowrap;
  }
  .job-state {
    flex: 0 0 72px;
    font-size: var(--size-meta);
    text-align: right;
  }
  .job-state.state-err {
    color: var(--err);
  }
  .job-state.state-ok {
    color: var(--ok);
  }
  .job-state.state-attn {
    color: var(--attn-text);
  }
  .job-state.state-idle {
    color: var(--muted);
  }

  .job-detail {
    padding: 12px 16px 16px;
    border-bottom: 1px solid var(--line);
    background: var(--page-bg);
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .detail-head {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .detail-kind {
    color: var(--muted);
    font-size: var(--size-meta);
  }
  .detail-repo {
    text-decoration: none;
  }
  .detail-repo:hover {
    text-decoration: underline;
  }
  .detail-label {
    margin: 4px 0 0;
    color: var(--muted);
    font-size: var(--size-meta);
    font-weight: 600;
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }
  .detail-pre {
    max-height: 240px;
    margin: 0;
    padding: 10px 12px;
    overflow: auto;
    border: 1px solid var(--line);
    border-radius: var(--radius-md);
    background: var(--panel-bg);
    font-family: var(--font-mono);
    font-size: var(--size-body-mono);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .result-md {
    color: var(--text-soft);
    font-size: var(--size-body);
    line-height: 1.6;
    overflow-wrap: anywhere;
  }
  .result-md :global(p) {
    margin: 0 0 8px;
  }
  .result-md :global(p:last-child) {
    margin-bottom: 0;
  }
  .result-md :global(h2),
  .result-md :global(h3) {
    margin: 16px 0 8px;
    color: var(--text);
    font-size: var(--size-title);
  }
  .result-md :global(h3) {
    font-size: var(--size-body);
  }
  .result-md :global(ul),
  .result-md :global(ol) {
    margin: 8px 0;
    padding-left: 22px;
  }
  .result-md :global(li) {
    margin: 4px 0;
  }
  .result-md :global(code) {
    padding: 2px 4px;
    border-radius: 3px;
    background: var(--code-bg);
    font: var(--size-body-mono) var(--font-mono);
  }
  .result-md :global(pre) {
    margin: 8px 0;
    padding: 12px 14px;
    overflow-x: auto;
    border: 0;
    border-radius: var(--radius-lg);
    background: var(--code-bg);
    font-size: var(--size-body-mono);
    line-height: 1.55;
  }
  .result-md :global(pre code) {
    padding: 0;
    background: none;
  }
  .result-md :global(a) {
    color: var(--info);
  }
  .result-md :global(blockquote) {
    margin: 8px 0;
    padding: 4px 12px;
    border-left: 1px solid var(--line-strong);
    color: var(--muted);
  }
  .result-md :global(table) {
    margin: 8px 0;
    border-collapse: collapse;
  }
  .result-md :global(th),
  .result-md :global(td) {
    padding: 4px 8px;
    border: 1px solid var(--line);
    text-align: left;
  }
  .pr-ref {
    position: absolute;
    bottom: 8px;
    left: 32px;
    padding: 4px 8px;
    color: var(--info);
    background: var(--panel-bg);
    border: 1px solid var(--line);
    border-radius: var(--radius-sm);
    text-decoration: none;
  }
  .pr-ref:hover {
    text-decoration: underline;
  }
  .pr-card {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 12px;
    border: 1px solid var(--line);
    border-radius: var(--radius-md);
    background: var(--panel-bg);
  }
  .pr-card-head,
  .pr-stats {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .pr-number {
    color: var(--info);
    font-weight: 700;
    text-decoration: none;
  }
  .pr-number:hover {
    text-decoration: underline;
  }
  .pr-state {
    padding: 2px 7px;
    border-radius: var(--radius-pill);
    font-size: var(--size-meta);
  }
  .pr-state-open {
    color: var(--ok);
    background: var(--ok-soft);
  }
  .pr-state-closed {
    color: var(--err);
    background: var(--err-bg);
  }
  .pr-state-merged {
    color: var(--attn-text);
    background: var(--attn-soft);
  }
  .pr-title {
    color: var(--text);
    font-size: var(--size-body);
    font-weight: 600;
  }
  .pr-stats {
    color: var(--muted);
    font-size: var(--size-meta);
  }
  .pr-add {
    color: var(--diff-add-mark);
  }
  .pr-del {
    color: var(--diff-del-mark);
  }
  .attempt {
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding-top: 6px;
  }
  .attempt-head {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 6px;
    color: var(--text-soft);
    font-size: var(--size-body-mono);
  }
  .session-link {
    color: var(--info);
    font-size: var(--size-meta);
    text-decoration: none;
  }
  .session-link:hover {
    text-decoration: underline;
  }
  .activities {
    max-height: 320px;
    margin: 0;
    padding: 10px 12px 10px 40px;
    overflow: auto;
    border: 1px solid var(--line);
    border-radius: var(--radius-md);
    background: var(--panel-bg);
    color: var(--text-soft);
    font-size: var(--size-body-mono);
  }
  .activities li {
    overflow-wrap: anywhere;
  }
  .activities li::marker {
    color: var(--muted);
  }
  .quiet-note {
    margin: 0;
    padding: 8px 12px;
    color: var(--muted);
    font-size: var(--size-meta);
  }

  @media (max-width: 760px) {
    .job-calls {
      display: none;
    }
    .job-meta {
      display: none;
    }
    .job-state {
      flex-basis: 56px;
    }
    .hint {
      display: none;
    }
  }
</style>
