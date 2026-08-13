<script>
  import { fmtCost, fmtDur } from "./run-format.js";
  import { runActivityAt } from "./run-history.js";
  import { nodeIconKey, nodeStateClass } from "./dag.js";
  import { RUN_LEXICON as P } from "./run-lexicon.js";
  import PaneHeader from "./PaneHeader.svelte";
  import StateIcon from "./StateIcon.svelte";

  let {
    master,
    activity = [],
    newSession = { prompt: "", model: "", repo: "", branch: "" },
    newRun = { budget: "", idempotencyKey: "" },
    repos = [],
    branches = [],
    repoLoading = false,
    branchLoading = false,
    modelPicker,
    onChangeSession = () => {},
    onChangeRun = () => {},
    onLoadBranches = () => {},
    onCreateSession = () => {},
    onCreateRun = () => {},
    onSelectRun = () => {},
    onSelectSession = () => {},
    relativeTime = () => "unknown",
    onStartRun = () => {},
    view = { engine_tier: "live", snapshot_age_seconds: 0 },
  } = $props();

  const total = $derived(
    master.runs.reduce((sum, run) => sum + Number(run.cost_usd || 0), 0),
  );
  // This is intentionally seven days. The sidebar's RECENT_HISTORY_MS is a
  // 24-hour navigation convenience, while this is the home activity summary.
  const RECENT_ACTIVITY_MS = 7 * 24 * 60 * 60 * 1000;
  let mode = $state("session");
  const recentAll = $derived(
    activity
      .map((item) => ({ ...item, at: activityAt(item) }))
      .filter((item) => item.at && Date.now() - item.at < RECENT_ACTIVITY_MS)
      .sort((a, b) => b.at - a.at),
  );
  const recent = $derived(recentAll.slice(0, 10));
  const sessionCount = $derived(
    recentAll.filter((item) => item.kind === "session").length,
  );
  const runCount = $derived(
    recentAll.filter((item) => item.kind === "run").length,
  );
  const recentSpend = $derived(
    recentAll.reduce((sum, item) => sum + Number(item.cost || 0), 0),
  );

  function activityAt(item) {
    const value =
      item.kind === "run"
        ? runActivityAt(item.value)
        : (item.value?.last_turn_at ?? item.value?.created_at);
    const parsed = Date.parse(value ?? "");
    return Number.isFinite(parsed) ? parsed : null;
  }

  function activityState(item) {
    const status = item.value?.state ?? item.value?.status;
    if (status === "completed" || status === "approved" || status === "done")
      return "done";
    return status || "future";
  }

  function activityTitle(item) {
    return item.kind === "run"
      ? item.value?.title || item.value?.task?.text || P.labels.run
      : item.value?.title ||
          item.value?.prompt ||
          item.value?.local_session_id ||
          P.labels.sessionWord;
  }

  function submit() {
    if (mode === "run") onCreateRun();
    else onCreateSession();
  }
</script>

<div
  class:tier-stale={view.engine_tier === "stale"}
  class="runview master-view"
>
  <PaneHeader kind={P.labels.masterEyebrow}>
    {#snippet chips()}
      <span class="rv-id">{master.runs.length} {P.labels.inFlight}</span>
    {/snippet}
  </PaneHeader>
  {#if view.engine_tier === "absent"}
    <div class="m-quiet">{P.labels.absentNotice}</div>
  {:else}
    {#if master.runs.some((run) => run.needs)}<div class="att-band">
        <div class="col-label">{P.labels.attention}</div>
        {#each master.runs.filter((run) => run.needs) as run}<button
            class="att-row"
            type="button"
            aria-label={`${P.labels.run} ${run.title}${P.punct.colon} ${P.stateWords[run.state] || run.state}`}
            onclick={() => onSelectRun(run.workflow_id)}
          >
            <div class="att-title">
              {run.title}
              <span class="entry-meta">{P.stateWords[run.state]}</span>
            </div>
            <div class="att-reason">{run.needs.reason}</div>
          </button>{/each}
      </div>{:else}<div class="m-quiet">{P.labels.nothingNeedsYou}</div>{/if}

    <section class="launcher" aria-label={P.labels.launcherLabel}>
      <div class="launcher-head">
        <span class="col-label">{P.labels.launcherHeading}</span>
        <!-- Transitional until issue #4781 stage 1: the conductor model
             router does not exist yet. The target design is one task box. -->
        <div class="mode-toggle" role="group" aria-label={P.labels.mode}>
          <button
            class:on={mode === "session"}
            type="button"
            onclick={() => (mode = "session")}>{P.labels.sessionMode}</button
          >
          <button
            class:on={mode === "run"}
            type="button"
            onclick={() => (mode = "run")}>{P.labels.runMode}</button
          >
        </div>
      </div>
      <form
        onsubmit={(event) => {
          event.preventDefault();
          submit();
        }}
      >
        <textarea
          rows="4"
          value={newSession.prompt}
          placeholder={P.labels.launcherPromptPlaceholder}
          aria-label={P.labels.task}
          oninput={(event) =>
            onChangeSession("prompt", event.currentTarget.value)}
          onkeydown={(event) => {
            if (
              (event.metaKey || event.ctrlKey) &&
              event.key === "Enter" &&
              !event.isComposing
            ) {
              event.preventDefault();
              submit();
            }
          }}></textarea>
        <div class="launcher-fields">
          <div class="field">
            <span class="field-label">{P.labels.modelWord}</span
            >{@render modelPicker(newSession.model, (value) =>
              onChangeSession("model", value),
            )}
          </div>
          <label
            >{P.labels.repoWord}<select
              class="mono"
              value={newSession.repo}
              disabled={repoLoading}
              onchange={(event) => {
                const value = event.currentTarget.value;
                onChangeSession("repo", value);
                onChangeSession("branch", "");
                onLoadBranches(value);
              }}
            >
              {#if repoLoading}<option value="">{P.labels.loadingRepos}</option
                >{:else}<option value=""
                  >{mode === "run"
                    ? P.labels.selectRepo
                    : P.labels.scratchWorkspace}</option
                >{#each repos as repo}<option value={repo.id}>{repo.id}</option
                  >{/each}{/if}
            </select></label
          >
          <label
            >{P.labels.branchWord}<select
              class="mono"
              value={newSession.branch}
              disabled={!newSession.repo || branchLoading}
              onchange={(event) =>
                onChangeSession("branch", event.currentTarget.value)}
            >
              {#if branchLoading}<option value=""
                  >{P.labels.loadingBranches}</option
                >{:else if branches.length === 0}<option value="main"
                  >main</option
                >{:else}{#each branches as branch}<option value={branch.name}
                    >{branch.name}</option
                  >{/each}{/if}
            </select></label
          >
          {#if mode === "run"}<label
              >{P.labels.budgetWord}<input
                type="number"
                min="0"
                step="0.01"
                value={newRun.budget}
                oninput={(event) =>
                  onChangeRun("budget", event.currentTarget.value)}
              /></label
            >{/if}
        </div>
        <button
          class="launcher-submit"
          type="submit"
          disabled={!newSession.prompt.trim() ||
            (mode === "run" && (!newSession.repo || !newSession.branch))}
          >{mode === "run"
            ? P.labels.launcherStartRun
            : P.labels.launcherStartSession}</button
        >
      </form>
    </section>

    {#each master.queues as queue}<div class="queue-line">
        {queue.name}
        {P.labels.queueWord}
        {P.punct.dot}
        {queue.running}
        {P.labels.runningWord}
        {P.labels.of}
        {queue.concurrency}
        {P.punct.dot}
        {queue.waiting}
        {P.labels.waitingWord}
      </div>{/each}

    <section class="recent-activity" aria-label={P.labels.recentHeading}>
      <div class="col-label">{P.labels.recentHeading}</div>
      {#if recent.length}{#each recent as item}<button
            class="activity-row"
            type="button"
            onclick={() =>
              item.kind === "run"
                ? onSelectRun(item.value.workflow_id)
                : onSelectSession(item.value.id)}
          >
            <StateIcon
              icon={nodeIconKey({ state: activityState(item) })}
              class={nodeStateClass({ state: activityState(item) })}
            />
            <span class="activity-title">{activityTitle(item)}</span>
            <span class="activity-model"
              >{item.value?.model ||
                (item.kind === "run"
                  ? item.value?.plan?.implementer_model
                  : P.labels.defaultWord)}</span
            >
            <span class="activity-time"
              >{relativeTime(
                item.value?.completed_at ??
                  item.value?.updated_at ??
                  item.value?.last_turn_at ??
                  item.value?.created_at,
              )}</span
            >
            <span class="activity-cost">{fmtCost(item.cost) || "$0.00"}</span>
          </button>{/each}<a class="activity-more" href="?"
          >{P.labels.viewMoreInSidebar}</a
        >{:else}<div class="m-quiet">{P.labels.noRecentActivity}</div>{/if}
      <div class="activity-footer">
        {P.labels.last7Days}
        {P.punct.dot}
        {sessionCount}
        {P.labels.recentSessionsCount}
        {P.punct.dot}
        {runCount}
        {P.labels.recentRunsCount}
        {P.punct.dot}
        {fmtCost(recentSpend) || "$0.00"}
      </div>
    </section>

    <div class="m-totals">
      {P.labels.spend}{P.punct.colon}
      {fmtCost(total) || "$0.00"}{P.punct.dot}
      {master.runs.length}
      {P.labels.runsWord}
    </div>
    {#if view.engine_tier === "stale"}<div class="rv-foot">
        {P.labels.engine}{P.punct.colon}
        {P.labels.staleShowing}
        {fmtDur(view.snapshot_age_seconds)}
        {P.labels.staleOld}
      </div>{/if}
  {/if}
</div>

<style>
  .m-row,
  .att-row,
  .mode-toggle button,
  .launcher-submit,
  .activity-row {
    color: inherit;
    background: transparent;
    border: 0;
    text-align: left;
    font: inherit;
    cursor: pointer;
  }
  .m-row,
  .att-row {
    width: 100%;
  }
  .launcher {
    margin-top: 16px;
  }
  .launcher-head,
  .launcher-fields {
    display: flex;
    gap: 8px;
    align-items: center;
    flex-wrap: wrap;
  }
  .launcher-head {
    justify-content: space-between;
    margin-bottom: 8px;
  }
  .launcher textarea,
  .launcher select,
  .launcher input {
    box-sizing: border-box;
    width: 100%;
    color: var(--text);
    background: var(--panel-bg);
    border: 1px solid var(--line-strong);
    padding: 8px;
  }
  .launcher textarea {
    resize: vertical;
  }
  .launcher-fields {
    align-items: end;
    margin-top: 8px;
  }
  .launcher-fields > label,
  .launcher-fields > .field {
    min-width: 120px;
    flex: 1;
  }
  .field-label,
  .launcher-fields label {
    display: grid;
    gap: 4px;
    font-size: var(--size-meta);
  }
  .mode-toggle {
    display: flex;
    border: 1px solid var(--line-strong);
  }
  .mode-toggle button {
    padding: 4px 8px;
    font-size: var(--size-meta);
  }
  .mode-toggle button.on {
    background: var(--hover);
  }
  .launcher-submit {
    margin-top: 10px;
    padding: 8px 12px;
    color: var(--ink-text);
    background: var(--ink);
    border: 1px solid var(--ink);
    text-align: center;
  }
  .launcher-submit:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .recent-activity {
    margin-top: 18px;
  }
  .activity-row {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto auto auto;
    gap: 8px;
    align-items: baseline;
    width: 100%;
    padding: 7px 2px;
    border-bottom: 1px solid var(--line);
  }
  .activity-title {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .activity-model,
  .activity-time,
  .activity-cost,
  .activity-footer,
  .activity-more {
    color: var(--text-soft);
    font: var(--size-meta) var(--font-mono);
  }
  .activity-more {
    display: inline-block;
    margin-top: 8px;
    color: var(--info);
  }
  .activity-footer {
    margin-top: 12px;
  }
  .m-empty {
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .tier-stale .activity-row,
  .tier-stale .att-row,
  .tier-stale .queue-line {
    filter: grayscale(0.6);
    opacity: 0.85;
  }
  @media (max-width: 520px) {
    .activity-row {
      grid-template-columns: auto minmax(0, 1fr) auto;
    }
    .activity-model {
      display: none;
    }
    .activity-cost {
      grid-column: 2;
    }
    .launcher-fields > label,
    .launcher-fields > .field {
      min-width: 100%;
    }
  }
</style>
