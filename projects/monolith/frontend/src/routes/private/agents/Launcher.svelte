<script>
  import { nodeStateClass } from "./dag.js";
  import { firstLine, fmtCost } from "./run-format.js";
  import { relativeTime } from "./run-history.js";
  import { RUN_LEXICON as P } from "./run-lexicon.js";
  import { sessionTitle } from "./jump.js";
  import { statusClass, statusLabel } from "./status.js";

  let {
    session = $bindable(),
    models = [],
    repos = [],
    repoLoading = false,
    branchLoading = false,
    creating = false,
    summary = {
      items: [],
      count: 0,
      allCount: 0,
      sessionCount: 0,
      spend: 0,
    },
    onLoadBranches = () => {},
    onSubmit = () => {},
    onOpenRun = () => {},
    onOpenSession = () => {},
    onOpenJump = () => {},
  } = $props();

  const modelName = $derived(session.model || P.labels.defaultModel);

  function title(item) {
    return item.kind === "run"
      ? firstLine(item.value?.title || item.value?.task?.text) ||
          item.value?.workflow_id
      : sessionTitle(item.value);
  }

  function shapeClass(run, node) {
    if (
      run?.needs?.kind === "human" &&
      node.state === "blocked" &&
      run.current?.state === "blocked"
    ) {
      return "g-blocked-h";
    }
    return nodeStateClass(node);
  }

  function submit() {
    if (!creating && session.prompt.trim()) onSubmit();
  }
</script>

<section class="home">
  <h1>
    {P.labels.launcherQuestion.replace("{model}", modelName)}
  </h1>

  <form
    class="launcher-form"
    onsubmit={(event) => {
      event.preventDefault();
      submit();
    }}
  >
    <div class="box">
      <textarea
        bind:value={session.prompt}
        rows="4"
        placeholder={P.labels.launcherPlaceholder}
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
      <div class="bar">
        <label class="control">
          <span class="sr-only">{P.labels.modelPicker}</span>
          <select
            class="select mono"
            aria-label={P.labels.modelPicker}
            bind:value={session.model}
          >
            <option value="">{P.labels.defaultWord}</option>
            {#if session.model && !models.includes(session.model)}
              <option value={session.model}>{session.model}</option>
            {/if}
            {#each models as model}<option value={model}>{model}</option>{/each}
          </select>
        </label>
        <label class="control repo-control">
          <span class="sr-only">{P.labels.repoWord}</span>
          <select
            class="select mono"
            aria-label={P.labels.repoWord}
            bind:value={session.repo}
            disabled={repoLoading || branchLoading}
            onchange={() => {
              session.branch = "";
              onLoadBranches(session.repo);
            }}
          >
            {#if repoLoading}
              <option value="">{P.labels.loadingRepos}</option>
            {:else}
              <option value="">{P.labels.noRepo}</option>
              {#each repos as repo}
                <option value={repo.id} title={repo.description || ""}>
                  {repo.id}{session.repo === repo.id
                    ? `@${branchLoading ? P.labels.loadingBranches : session.branch || P.labels.defaultBranch}`
                    : ""}
                </option>
              {/each}
            {/if}
          </select>
        </label>
        <span class="hint mono">{P.labels.startHint}</span>
        <button
          class="send"
          type="submit"
          aria-label={creating ? P.labels.creating : P.labels.startTask}
          disabled={creating || !session.prompt.trim()}
        >
          <svg viewBox="0 0 18 18" aria-hidden="true">
            <path d="M9 14V4m0 0L5 8m4-4 4 4"></path>
          </svg>
        </button>
      </div>
    </div>
  </form>

  <div class="recent">
    <div class="group-title">
      <span>{P.labels.recentHeading}</span>
      <span class="recent-summary mono">
        {P.labels.recentWindow}
        {P.punct.dot}
        {summary.sessionCount}
        {P.labels.recentSessionsCount}
        {P.punct.dot}
        {fmtCost(summary.spend) || P.labels.zeroCost}
      </span>
    </div>
    <div class="row-list">
      {#each summary.items as item (`${item.kind}:${item.id}`)}
        {@const entry = item.value}
        <button
          class="row"
          type="button"
          aria-label={item.kind === "run"
            ? `${title(item)}: ${P.stateWords[entry.state] || entry.state}`
            : `${title(item)}: ${statusLabel(entry)}`}
          onclick={() =>
            item.kind === "run" ? onOpenRun(item.id) : onOpenSession(item.id)}
        >
          {#if item.kind === "run"}
            <span class="run-shape-strip" aria-hidden="true">
              {#each entry.shape?.length ? entry.shape : [{ key: "run", kind: "work", state: entry.state }] as node, index (`${node.key}:${index}`)}
                <span
                  class:gate={node.kind === "gate"}
                  class={`shape-node ${shapeClass(entry, node)}`}
                ></span>
              {/each}
            </span>
          {:else}
            <span class={`dot ${statusClass(entry)}`} title={statusLabel(entry)}
            ></span>
          {/if}
          <span class="main">
            <span class="row-title">{title(item)}</span>
            <span class="row-sub mono">
              {#if item.kind === "run"}
                {P.labels.run}
                {P.punct.dot}
                {P.stateWords[entry.state] || entry.state}
                {P.punct.dot}
                {fmtCost(entry.cost_usd) || P.labels.zeroCost}
              {:else}
                {entry.model || P.labels.defaultModel}
                {P.punct.dot}
                {entry.repo
                  ? `${entry.repo}@${entry.branch || P.labels.defaultBranch}`
                  : P.labels.noRepo}
              {/if}
            </span>
          </span>
          <span class="age mono">{relativeTime(item.activityAt)}</span>
        </button>
      {:else}
        <div class="empty">{P.labels.noRecentActivity}</div>
      {/each}
    </div>
    <button class="more-link" type="button" onclick={onOpenJump}>
      <span
        >{P.labels.allInJump.replace(
          "{count}",
          String(summary.allCount ?? summary.count),
        )}</span
      >
      <kbd class="kbd">{P.labels.shortcutCommandK}</kbd>
    </button>
  </div>
</section>

<style>
  .home {
    width: 100%;
    max-width: 720px;
    margin: 0 auto;
    padding: 48px 0 32px;
    display: flex;
    flex-direction: column;
    gap: 28px;
  }
  h1 {
    margin: 0;
    color: var(--text);
    font-size: 20px;
    font-weight: 600;
    letter-spacing: -0.01em;
  }
  .box {
    border: 1px solid var(--line-strong);
    border-radius: 6px;
    background: var(--panel-bg);
  }
  .box:focus-within {
    border-color: var(--info);
    outline: 2px solid var(--info);
    outline-offset: 1px;
  }
  textarea {
    width: 100%;
    min-height: 96px;
    resize: none;
    padding: 12px 14px 6px;
    border: 0;
    border-radius: 0;
    color: var(--text);
    background: transparent;
    font: 15px/1.5 var(--font-ui);
    outline: none;
  }
  .bar {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 4px 8px 8px 10px;
  }
  .control {
    position: relative;
    flex: 0 0 auto;
  }
  .control::after {
    position: absolute;
    top: 50%;
    right: 7px;
    color: var(--muted);
    content: "▾";
    font: 12px var(--font-mono);
    pointer-events: none;
    transform: translateY(-55%);
  }
  .select {
    width: auto;
    min-width: 62px;
    height: 28px;
    padding: 0 24px 0 7px;
    appearance: none;
    border: 0;
    border-radius: var(--radius-md);
    color: var(--text-soft);
    background: transparent;
    font: 12px var(--font-mono);
  }
  .select:hover {
    background: var(--hover);
  }
  .select:focus-visible,
  textarea:focus-visible,
  button:focus-visible {
    outline: 2px solid var(--info);
    outline-offset: 2px;
  }
  .repo-control {
    min-width: 0;
    max-width: 280px;
  }
  .repo-control .select {
    max-width: 100%;
  }
  .hint {
    margin-left: auto;
    color: var(--muted);
    font-size: 11.5px;
    white-space: nowrap;
  }
  .send {
    width: 32px;
    height: 32px;
    display: inline-flex;
    flex: 0 0 32px;
    align-items: center;
    justify-content: center;
    padding: 0;
    border: 1px solid var(--ink);
    border-radius: 4px;
    color: var(--ink-text);
    background: var(--ink);
  }
  .send svg {
    width: 18px;
    height: 18px;
    fill: none;
    stroke: currentColor;
    stroke-width: 1.5;
    stroke-linecap: round;
    stroke-linejoin: round;
  }
  .send:disabled {
    cursor: not-allowed;
    opacity: 0.45;
  }
  .recent {
    min-width: 0;
  }
  .group-title {
    display: flex;
    justify-content: space-between;
    gap: 16px;
    margin: 0;
    padding: 0 12px 6px;
    color: var(--muted);
    font-size: 11.5px;
    font-weight: 600;
    line-height: 1.2;
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }
  .recent-summary {
    overflow: hidden;
    font-weight: 400;
    letter-spacing: 0;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .row-list {
    display: grid;
    gap: 2px;
  }
  .row {
    width: 100%;
    min-width: 0;
    min-height: 52px;
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 8px 12px;
    border: 1px solid transparent;
    border-radius: 6px;
    color: inherit;
    background: transparent;
    text-align: left;
  }
  .row:hover,
  .more-link:hover {
    background: var(--hover);
  }
  .dot {
    width: 8px;
    height: 8px;
    flex: 0 0 8px;
    border-radius: var(--radius-circle);
    background: var(--dot-idle);
  }
  .dot.running,
  .dot.working {
    background: var(--ok);
  }
  .dot.needs_input {
    background: var(--attn);
  }
  .dot.warn {
    background: var(--err);
  }
  .run-shape-strip {
    flex: 0 0 auto;
    display: inline-flex;
    align-items: center;
    gap: 3px;
    color: var(--muted);
  }
  .shape-node {
    width: 7px;
    height: 7px;
    flex: 0 0 7px;
    border-radius: 2px;
    background: currentColor;
  }
  .shape-node.gate {
    transform: rotate(45deg) scale(0.85);
  }
  .main {
    min-width: 0;
    flex: 1;
    display: grid;
    gap: 3px;
  }
  .row-title,
  .row-sub {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .row-title {
    color: var(--text);
    font-size: 14px;
    font-weight: 500;
  }
  .row-sub,
  .age,
  .empty {
    color: var(--muted);
    font-size: 12px;
  }
  .age {
    flex: 0 0 44px;
    width: 44px;
    text-align: right;
    white-space: nowrap;
  }
  .empty {
    padding: 12px;
  }
  .more-link {
    width: 100%;
    height: 36px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-top: 10px;
    padding: 0 12px;
    border: 0;
    border-radius: var(--radius-md);
    color: var(--muted);
    background: transparent;
    font-size: 12px;
    text-align: left;
  }
  .kbd {
    border: 0;
    color: var(--muted);
    background: transparent;
    font: 11.5px var(--font-mono);
    white-space: nowrap;
  }
  .mono {
    font-family: var(--font-mono);
  }
  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
  }
  @media (max-width: 760px) {
    .home {
      padding: 24px 4px 16px;
      gap: 24px;
    }
    .bar {
      flex-wrap: wrap;
    }
    .hint,
    .kbd {
      display: none;
    }
    .send {
      width: 44px;
      height: 44px;
      flex-basis: 44px;
      margin-left: auto;
    }
    .select {
      min-height: 44px;
    }
    .row {
      min-height: 60px;
    }
  }
</style>
