<script>
  import { goto } from "$app/navigation";
  import { page } from "$app/stores";
  import AccessPanel from "./AccessPanel.svelte";
  import {
    collisionWording,
    formatTaskDueDate,
    moveCountdown,
    progressSummary,
    sortOpenTasks,
    titleCaseName,
  } from "./moving.js";
  import "./moving-theme.css";

  let { data } = $props();

  let tasks = $state(data.state?.tasks ?? []);
  let progress = $state(data.state?.progress ?? 0);
  let pendingTaskIds = $state(new Set());
  let taskError = $state("");

  const currentScope = $derived(data.scope === "all" ? "all" : "mine");
  const countdown = $derived(moveCountdown(data.state?.spans ?? []));
  const openTasks = $derived(sortOpenTasks(tasks));
  const progressView = $derived(progressSummary(progress, tasks));
  const collisions = $derived(
    (data.state?.collisions ?? [])
      .map((collision) =>
        collisionWording(
          collision,
          data.state?.tasks ?? [],
          data.state?.spans ?? [],
        ),
      )
      .filter(Boolean),
  );

  $effect(() => {
    tasks = data.state?.tasks ?? [];
    progress = data.state?.progress ?? 0;
    taskError = "";
  });

  function setScope(scope) {
    if (scope === currentScope) return;
    const url = new URL($page.url);
    url.searchParams.set("scope", scope);
    goto(url, { keepFocus: true, noScroll: true });
  }

  function updateProgress() {
    const total = tasks.length;
    const done = tasks.filter((task) => task.done_at != null).length;
    progress = total === 0 ? 0 : done / total;
  }

  async function toggleTask(task) {
    if (pendingTaskIds.has(task.id)) return;

    const wasDone = task.done_at != null;
    pendingTaskIds = new Set(pendingTaskIds).add(task.id);
    taskError = "";
    tasks = tasks.map((item) =>
      item.id === task.id
        ? { ...item, done_at: wasDone ? null : new Date().toISOString() }
        : item,
    );
    updateProgress();

    try {
      const action = wasDone ? "undone" : "done";
      const response = await fetch(
        `/api/moving/tasks/${encodeURIComponent(task.id)}/${action}`,
        { method: "POST" },
      );
      if (!response.ok)
        throw new Error(`task update failed: ${response.status}`);
    } catch {
      tasks = tasks.map((item) =>
        item.id === task.id ? { ...item, done_at: task.done_at } : item,
      );
      updateProgress();
      taskError = "That task could not be updated. Please try again.";
    } finally {
      const remaining = new Set(pendingTaskIds);
      remaining.delete(task.id);
      pendingTaskIds = remaining;
    }
  }
</script>

<svelte:head>
  <title>Crossing | Moving planner</title>
  <meta
    name="description"
    content="Crossing, a shared moving plan for the road ahead."
  />
</svelte:head>

{#if data.status !== "ready"}
  <main class="moving access-shell">
    <AccessPanel status={data.status} />
  </main>
{:else}
  <main class="moving">
    <div class="moving-grid">
      <header class="masthead cell">
        <div>
          <p class="kicker">Moving planner</p>
          <h1>Crossing</h1>
        </div>
        <div class="viewer">
          <span>{titleCaseName(data.state.viewer)}</span>
          <span>{currentScope === "mine" ? "My plan" : "All plans"}</span>
        </div>
      </header>

      <section class="hero cell" aria-labelledby="move-countdown">
        <p class="section-label">The crossing</p>
        <h2 id="move-countdown">{countdown.headline}</h2>
        <p class="countdown-detail">{countdown.detail}</p>

        <div class="progress-heading">
          <span>Progress</span>
          <strong>{progressView.label}</strong>
        </div>
        <div
          class="progress-track"
          role="progressbar"
          aria-label="Move progress"
          aria-valuemin="0"
          aria-valuemax="100"
          aria-valuenow={progressView.percent}
        >
          <span style:width={`${progressView.percent}%`}></span>
        </div>
        {#if progressView.total === 0}
          <p class="empty-copy">No tasks yet. The first step can start here.</p>
        {/if}
      </section>

      <section class="today cell" aria-labelledby="today-heading">
        <div class="today-header">
          <div>
            <p class="section-label">Paper list</p>
            <h2 id="today-heading">Do Today</h2>
          </div>
          <div class="scope-toggle" aria-label="Task scope">
            <button
              type="button"
              class:active={currentScope === "mine"}
              aria-pressed={currentScope === "mine"}
              onclick={() => setScope("mine")}>Mine</button
            >
            <button
              type="button"
              class:active={currentScope === "all"}
              aria-pressed={currentScope === "all"}
              onclick={() => setScope("all")}>All</button
            >
          </div>
        </div>

        {#if taskError}
          <p class="task-error" role="alert">{taskError}</p>
        {/if}

        {#if openTasks.length > 0}
          <ul class="task-list">
            {#each openTasks as task (task.id)}
              <li>
                <label>
                  <input
                    type="checkbox"
                    checked={task.done_at != null}
                    disabled={pendingTaskIds.has(task.id)}
                    onchange={() => toggleTask(task)}
                  />
                  <span class="task-copy">
                    <strong>{task.title}</strong>
                    <span>{formatTaskDueDate(task.due_on)}</span>
                  </span>
                </label>
              </li>
            {/each}
          </ul>
        {:else}
          <div class="today-empty">
            <strong>Nothing waiting.</strong>
            <span>There are no unfinished tasks in this view.</span>
          </div>
        {/if}
      </section>

      <section class="collisions cell" aria-labelledby="collisions-heading">
        <p class="section-label">Heads up</p>
        <h2 id="collisions-heading">Collisions</h2>
        {#if collisions.length > 0}
          <ul>
            {#each collisions as collision}
              <li>{collision}</li>
            {/each}
          </ul>
        {:else}
          <p class="empty-copy">
            No date collisions. The plan has room to breathe.
          </p>
        {/if}
      </section>
    </div>
  </main>
{/if}

<style>
  .moving {
    width: 100%;
    height: 100dvh;
    overflow: auto;
    background: var(--moving-canvas);
    color: var(--moving-ink);
    font-family: var(--moving-font);
  }

  .access-shell {
    display: grid;
    min-width: 320px;
    place-items: center;
  }

  .moving-grid {
    display: grid;
    grid-template-columns: minmax(330px, 0.9fr) minmax(450px, 1.35fr);
    grid-template-rows: auto minmax(360px, 1fr) auto;
    grid-template-areas:
      "masthead masthead"
      "hero today"
      "collisions today";
    gap: var(--moving-border);
    width: max(100%, 784px);
    min-height: 100%;
    padding: var(--moving-border);
    background: var(--moving-ink);
  }

  .cell {
    min-width: 0;
    padding: clamp(20px, 3vw, 48px);
  }

  .masthead {
    grid-area: masthead;
    display: flex;
    align-items: end;
    justify-content: space-between;
    gap: 3rem;
    background: var(--moving-accent);
    color: var(--moving-accent-ink);
  }

  .masthead h1,
  .masthead p {
    margin: 0;
  }

  .masthead h1 {
    font-size: var(--moving-size-heading);
    font-weight: 950;
    letter-spacing: -0.06em;
    line-height: 0.85;
    text-transform: uppercase;
  }

  .kicker,
  .section-label {
    margin: 0 0 0.65rem;
    font-family: var(--moving-font-mono);
    font-size: var(--moving-size-small);
    font-weight: 800;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .viewer {
    display: flex;
    gap: 0.65rem;
    align-items: center;
    font-family: var(--moving-font-mono);
    font-size: var(--moving-size-small);
    font-weight: 800;
    text-transform: uppercase;
  }

  .viewer span + span {
    padding-left: 0.65rem;
    border-left: 2px solid var(--moving-accent-ink);
  }

  .hero {
    grid-area: hero;
    display: flex;
    flex-direction: column;
    background: var(--moving-hero);
  }

  .hero h2 {
    max-width: 9ch;
    margin: 0;
    font-size: var(--moving-size-display);
    font-weight: 950;
    letter-spacing: -0.075em;
    line-height: 0.82;
    text-transform: uppercase;
  }

  .countdown-detail {
    margin: 1.25rem 0 3rem;
    color: var(--moving-muted);
    font-family: var(--moving-font-mono);
    font-size: var(--moving-size-small);
    font-weight: 700;
  }

  .progress-heading {
    display: flex;
    justify-content: space-between;
    gap: 1rem;
    margin-top: auto;
    font-family: var(--moving-font-mono);
    font-size: var(--moving-size-small);
    text-transform: uppercase;
  }

  .progress-track {
    height: 20px;
    margin-top: 0.65rem;
    overflow: hidden;
    border: 3px solid var(--moving-ink);
    background: var(--moving-paper-raised);
  }

  .progress-track span {
    display: block;
    height: 100%;
    background: var(--moving-accent);
  }

  .empty-copy {
    margin: 1rem 0 0;
    color: var(--moving-muted);
    font-size: var(--moving-size-body);
    line-height: 1.45;
  }

  .today {
    grid-area: today;
    padding: 0;
    background: var(--moving-paper);
  }

  .today-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    padding: clamp(20px, 3vw, 42px);
    background: var(--moving-accent);
    color: var(--moving-accent-ink);
  }

  .today-header h2 {
    margin: 0;
    font-size: var(--moving-size-heading);
    font-weight: 950;
    letter-spacing: -0.05em;
    line-height: 0.9;
    text-transform: uppercase;
  }

  .scope-toggle {
    display: flex;
    border: 3px solid var(--moving-accent-ink);
  }

  .scope-toggle button {
    min-width: 60px;
    padding: 0.65rem 0.8rem;
    border: 0;
    background: var(--moving-accent);
    color: var(--moving-accent-ink);
    font-family: var(--moving-font-mono);
    font-size: var(--moving-size-small);
    font-weight: 900;
    text-transform: uppercase;
    cursor: pointer;
  }

  .scope-toggle button + button {
    border-left: 3px solid var(--moving-accent-ink);
  }

  .scope-toggle button.active {
    background: var(--moving-accent-ink);
    color: var(--moving-accent);
  }

  .scope-toggle button:focus-visible,
  input:focus-visible {
    outline: 3px solid var(--moving-focus);
    outline-offset: 3px;
  }

  .task-error {
    margin: 0;
    padding: 0.8rem clamp(20px, 3vw, 42px);
    background: var(--moving-danger-paper);
    color: var(--moving-danger);
    font-size: var(--moving-size-body);
    font-weight: 800;
  }

  .task-list {
    margin: 0;
    padding: 0 clamp(20px, 3vw, 42px);
  }

  .task-list li {
    border-bottom: 2px solid var(--moving-ink);
  }

  .task-list label {
    display: flex;
    gap: 1rem;
    align-items: flex-start;
    padding: 1.25rem 0;
    cursor: pointer;
  }

  .task-list input {
    flex: 0 0 auto;
    width: 25px;
    height: 25px;
    margin-top: 0.1rem;
    accent-color: var(--moving-check);
    cursor: pointer;
  }

  .task-copy {
    display: flex;
    min-width: 0;
    flex: 1;
    justify-content: space-between;
    gap: 1rem;
  }

  .task-copy strong {
    font-size: var(--moving-size-body);
    line-height: 1.3;
  }

  .task-copy span {
    flex: 0 0 auto;
    color: var(--moving-muted);
    font-family: var(--moving-font-mono);
    font-size: var(--moving-size-small);
    line-height: 1.6;
  }

  .today-empty {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    padding: clamp(28px, 5vw, 72px) clamp(20px, 3vw, 42px);
    font-size: var(--moving-size-body);
  }

  .today-empty strong {
    font-size: calc(var(--moving-size-body) * 1.35);
  }

  .today-empty span {
    color: var(--moving-muted);
  }

  .collisions {
    grid-area: collisions;
    background: var(--moving-collision);
  }

  .collisions h2 {
    margin: 0;
    font-size: calc(var(--moving-size-body) * 1.8);
    font-weight: 950;
    letter-spacing: -0.04em;
    text-transform: uppercase;
  }

  .collisions ul {
    display: grid;
    gap: 0.85rem;
    margin: 1.25rem 0 0;
    padding: 0;
  }

  .collisions li {
    padding-left: 1rem;
    border-left: 5px solid var(--moving-ink);
    font-size: var(--moving-size-body);
    font-weight: 750;
    line-height: 1.35;
  }
</style>
