<script>
  import { goto } from "$app/navigation";
  import { page } from "$app/stores";
  import { onMount } from "svelte";
  import AccessPanel from "./AccessPanel.svelte";
  import {
    collisionWording,
    formatCad,
    formatDateRange,
    formatShortDate,
    formatTaskDueDate,
    ganttDatePosition,
    ganttTimeline,
    mergeAgendaItems,
    moveCountdown,
    progressSummary,
    sortOpenTasks,
    sumSellValues,
    taskDueView,
    titleCaseName,
  } from "./moving.js";
  import "./moving-theme.css";

  let { data } = $props();

  let tasks = $state(data.state?.tasks ?? []);
  let progress = $state(data.state?.progress ?? 0);
  let pendingTaskIds = $state(new Set());
  let taskError = $state("");
  let isPhone = $state(false);
  let mobileTab = $state("today");
  let tasksDialog = $state();
  let sellDialog = $state();
  let calendarDialog = $state();
  let rolesDialog = $state();
  let plotCard = $state();
  let tooltip = $state(null);

  const now = new Date();
  const currentScope = $derived(data.scope === "all" ? "all" : "mine");
  const spans = $derived(data.state?.spans ?? []);
  const milestones = $derived(data.state?.milestones ?? []);
  const roles = $derived(data.state?.roles ?? []);
  const rawCollisions = $derived(data.state?.collisions ?? []);
  const viewer = $derived(data.state?.viewer ?? "");
  const countdown = $derived(moveCountdown(spans, now));
  const openTasks = $derived(sortOpenTasks(tasks));
  const progressView = $derived(progressSummary(progress, tasks));
  const sellTasks = $derived(tasks.filter((task) => task.track === "sell"));
  const sellTotal = $derived(sumSellValues(tasks));
  const timeline = $derived(ganttTimeline(spans, milestones, now));
  const agendaItems = $derived(
    mergeAgendaItems(milestones, spans, rawCollisions, tasks),
  );
  const todayRows = $derived(
    openTasks.map((task) => ({ task, due: taskDueView(task.due_on, now) })),
  );
  const overdueRows = $derived(
    todayRows.filter(({ due }) => due.bucket === "overdue"),
  );
  const weekRows = $derived(
    todayRows.filter(({ due }) => due.bucket === "week"),
  );
  const collisionRows = $derived(
    rawCollisions
      .map((collision) => ({
        ...collision,
        wording: collisionWording(collision, tasks, spans),
      }))
      .filter((collision) => collision.wording),
  );
  const milestonePoints = $derived(
    milestones
      .map((milestone) => ({
        ...milestone,
        position: ganttDatePosition(
          milestone.occurs_on,
          timeline.startsOn,
          timeline.endsOn,
        ),
      }))
      .filter((milestone) => milestone.position != null),
  );
  const legSpans = $derived(
    spans
      .filter((span) => span.kind === "move" || span.kind === "trip")
      .toSorted((left, right) => left.starts_on.localeCompare(right.starts_on)),
  );
  const currentDateLabel = new Intl.DateTimeFormat("en-GB", {
    weekday: "short",
    day: "numeric",
    month: "short",
  }).format(now);

  $effect(() => {
    tasks = data.state?.tasks ?? [];
    progress = data.state?.progress ?? 0;
    taskError = "";
  });

  onMount(() => {
    const narrow = matchMedia("(max-width: 700px)");
    const applyPhone = () => {
      isPhone = narrow.matches;
    };
    applyPhone();
    narrow.addEventListener("change", applyPhone);
    return () => narrow.removeEventListener("change", applyPhone);
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
      if (!response.ok) {
        throw new Error(`task update failed: ${response.status}`);
      }
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

  function openDialog(dialog) {
    for (const item of [tasksDialog, sellDialog, calendarDialog, rolesDialog]) {
      if (item?.open) item.close();
    }
    dialog?.showModal();
  }

  function closeFromBackdrop(event) {
    if (event.target === event.currentTarget) event.currentTarget.close();
  }

  function showTooltip(event, title, meta, detail = "") {
    if (!plotCard) return;
    const stage = plotCard.getBoundingClientRect();
    const target = event.currentTarget.getBoundingClientRect();
    tooltip = {
      title,
      meta,
      detail,
      left: Math.min(
        Math.max(target.left + target.width / 2 - stage.left, 100),
        stage.width - 100,
      ),
      top: target.top - stage.top,
    };
  }

  function taskHueClass(task) {
    return `hue-${task.track ?? "none"}`;
  }

  function spanHueClass(span) {
    return `hue-${span.kind}`;
  }

  function calendarStateClass(milestone) {
    if (milestone.gcal_state === "held") return "warn";
    if (milestone.gcal_state === "synced") return "ok";
    return "idle";
  }

  function calendarStateIcon(milestone) {
    if (milestone.gcal_state === "held") return "!";
    if (milestone.gcal_state === "synced") return "✓";
    return "○";
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
    <div class:phone={isPhone} class="app" data-mtab={mobileTab}>
      <div class="grid fx">
        <header class="cell c-head">
          <b>Crossing</b>
          <span>{titleCaseName(viewer)}'s plan</span>
          <time class="r" datetime={now.toISOString()}>{currentDateLabel}</time>
        </header>

        <section class="cell c-hero" aria-labelledby="move-countdown">
          {#if countdown.days == null}
            <div class="hero-empty" id="move-countdown">
              <strong>{countdown.headline}</strong>
              <span>{countdown.detail}</span>
            </div>
          {:else if countdown.days > 0}
            <h1 id="move-countdown">
              <span class="hl"
                >{countdown.days} {countdown.days === 1 ? "day" : "days"}</span
              >&nbsp;to wheels&#8209;up.
            </h1>
          {:else if countdown.days === 0}
            <h1 id="move-countdown">
              <span class="hl">Wheels&#8209;up</span>&nbsp;today.
            </h1>
          {:else}
            <h1 id="move-countdown">
              <span class="hl"
                >{Math.abs(countdown.days)}
                {Math.abs(countdown.days) === 1 ? "day" : "days"}</span
              >&nbsp;since wheels&#8209;up.
            </h1>
          {/if}
        </section>

        <section class="cell c-today" aria-labelledby="today-heading">
          <div class="ch">
            <h2 id="today-heading">Do today</h2>
            <span class="seg-mini" role="group" aria-label="Task scope">
              <button
                type="button"
                aria-pressed={currentScope === "mine"}
                onclick={() => setScope("mine")}>Mine</button
              >
              <button
                type="button"
                aria-pressed={currentScope === "all"}
                onclick={() => setScope("all")}>All</button
              >
            </span>
            <span class="badge"
              >{overdueRows.length
                ? `${overdueRows.length} late`
                : "clear"}</span
            >
          </div>
          <div class="cta-body">
            {#if taskError}
              <p class="task-error" role="alert">{taskError}</p>
            {/if}
            {#if overdueRows.length > 0}
              <div class="nsec-h">Late, clear these first</div>
              {#each overdueRows as row (row.task.id)}
                <div class="nrow">
                  <input
                    id={`today-${row.task.id}`}
                    type="checkbox"
                    checked={row.task.done_at != null}
                    disabled={pendingTaskIds.has(row.task.id)}
                    onchange={() => toggleTask(row.task)}
                  />
                  <label class="t" for={`today-${row.task.id}`}>
                    {row.task.title}
                    {#if row.task.owner !== viewer}
                      <span class="w">{titleCaseName(row.task.owner)}</span>
                    {/if}
                  </label>
                  <span class="due overdue">{row.due.label}</span>
                </div>
              {/each}
            {/if}
            {#if weekRows.length > 0}
              <div class="nsec-h">Then, this week</div>
              {#each weekRows as row (row.task.id)}
                <div class="nrow">
                  <input
                    id={`today-${row.task.id}`}
                    type="checkbox"
                    checked={row.task.done_at != null}
                    disabled={pendingTaskIds.has(row.task.id)}
                    onchange={() => toggleTask(row.task)}
                  />
                  <label class="t" for={`today-${row.task.id}`}>
                    {row.task.title}
                    {#if row.task.owner !== viewer}
                      <span class="w">{titleCaseName(row.task.owner)}</span>
                    {/if}
                  </label>
                  <span class="due">{row.due.label}</span>
                </div>
              {/each}
            {/if}
            {#if overdueRows.length === 0 && weekRows.length === 0}
              <p class="ncalm">Nothing waiting in the next seven days.</p>
            {/if}
            <button
              class="allbtn"
              type="button"
              onclick={() => openDialog(tasksDialog)}
              >All {openTasks.length} tasks →</button
            >
          </div>
        </section>

        <section
          class="cell card-plot"
          aria-label={`Timeline, ${formatShortDate(timeline.startsOn)} to ${formatShortDate(timeline.endsOn)}`}
          bind:this={plotCard}
        >
          <div class="plot">
            <div class="plot-scale">
              {#each timeline.months.slice(1) as month (month.value)}
                <span class="vline" style:left={`${month.position}%`}></span>
              {/each}
              {#each legSpans as span (span.id)}
                <div
                  class={`legtag ${spanHueClass(span)}`}
                  style:left={`${ganttDatePosition(span.starts_on, timeline.startsOn, timeline.endsOn)}%`}
                >
                  <b>{span.label}</b>
                  <span>{formatDateRange(span.starts_on, span.ends_on)}</span>
                </div>
              {/each}
              <div class="baseline"></div>
              <div class="today" style:left={`${timeline.todayPosition}%`}>
                <i></i><span class="tlbl">Today</span>
              </div>
              <div class="axis">
                {#each timeline.months as month (month.value)}
                  <span class="m" style:left={`${month.position}%`}
                    >{month.label}</span
                  >
                {/each}
              </div>
            </div>
            <div class="rows">
              <div class="row slim">
                <span class="name">Milestones</span>
                <div class="track">
                  {#each milestonePoints as milestone (milestone.id)}
                    <button
                      type="button"
                      class:fill={milestone.gcal_state === "synced"}
                      class:held={milestone.gcal_state === "held"}
                      class="dia"
                      style:left={`${milestone.position}%`}
                      aria-label={`${milestone.title}, ${formatShortDate(milestone.occurs_on)}, ${milestone.gcal_state}`}
                      onpointerenter={(event) =>
                        showTooltip(
                          event,
                          milestone.title,
                          `${formatShortDate(milestone.occurs_on)} · ${titleCaseName(milestone.owner)}`,
                          milestone.gcal_state,
                        )}
                      onpointerleave={() => (tooltip = null)}
                      onfocus={(event) =>
                        showTooltip(
                          event,
                          milestone.title,
                          `${formatShortDate(milestone.occurs_on)} · ${titleCaseName(milestone.owner)}`,
                          milestone.gcal_state,
                        )}
                      onblur={() => (tooltip = null)}
                      onclick={() => openDialog(calendarDialog)}
                    ></button>
                  {/each}
                </div>
              </div>
              {#each timeline.lanes as lane (lane.kind)}
                <div class="row">
                  <span class="name">{lane.label}</span>
                  <div class="track">
                    {#each lane.bars as bar (bar.id)}
                      <button
                        type="button"
                        class="bar"
                        style:left={`${bar.position}%`}
                        style:width={`${bar.width}%`}
                        aria-label={`${bar.label}, ${formatDateRange(bar.starts_on, bar.ends_on)}`}
                        onpointerenter={(event) =>
                          showTooltip(
                            event,
                            bar.label,
                            formatDateRange(bar.starts_on, bar.ends_on),
                          )}
                        onpointerleave={() => (tooltip = null)}
                        onfocus={(event) =>
                          showTooltip(
                            event,
                            bar.label,
                            formatDateRange(bar.starts_on, bar.ends_on),
                          )}
                        onblur={() => (tooltip = null)}
                      >
                        <span class="blbl">{bar.label}</span>
                      </button>
                    {/each}
                  </div>
                </div>
              {/each}
              {#if timeline.lanes.length === 0 && milestonePoints.length === 0}
                <p class="plot-empty">No timeline dates yet.</p>
              {/if}
            </div>
          </div>
          {#if tooltip}
            <div
              class="tip shown"
              role="status"
              aria-live="polite"
              style:left={`${tooltip.left}px`}
              style:top={`${tooltip.top}px`}
            >
              <b>{tooltip.title}</b><span>{tooltip.meta}</span>
              {#if tooltip.detail}<em>{tooltip.detail}</em>{/if}
            </div>
          {/if}
        </section>

        <section class="cell c-agenda" aria-label="The plan, as a list">
          <div class="ch">The plan</div>
          <div class="agenda-body">
            {#if agendaItems.length === 0}
              <p class="quiet-empty">No dated plan items yet.</p>
            {:else}
              {#each agendaItems as item, index (item.id)}
                {#if index === 0 || agendaItems[index - 1].monthKey !== item.monthKey}
                  <div class="amonth">{item.monthLabel}</div>
                {/if}
                <div
                  class:ms={item.kind === "ms"}
                  class:held={item.held}
                  class:col={item.kind === "col"}
                  class="arow"
                >
                  <span class="d">{formatShortDate(item.date)}</span>
                  <span class="ic" aria-hidden="true">{item.icon}</span>
                  <span>
                    {#if item.kind === "col"}<b>{item.title}</b
                      >{:else}{item.title}{/if}
                    <span class="sub">{item.sub}</span>
                  </span>
                </div>
              {/each}
            {/if}
          </div>
        </section>

        <section class="cell c-legsmini" aria-label="Leg dates">
          <div class="nsec-h">Leg dates</div>
          {#if legSpans.length === 0}
            <p class="quiet-empty">No legs scheduled.</p>
          {:else}
            {#each legSpans as span (span.id)}
              <div class="lm">
                <span class={`sw ${spanHueClass(span)}`} aria-hidden="true"
                ></span>
                <span>{span.label}</span>
                <span class="d"
                  >{formatDateRange(span.starts_on, span.ends_on)}</span
                >
              </div>
            {/each}
          {/if}
        </section>

        <section class="cell c-prog" aria-label="Progress">
          <div class="ch">Behind us</div>
          <div class="prog-body">
            <div class="prog-pct">{progressView.percent}%</div>
            <div class="prog-right">
              <div
                class="prog-bar"
                role="progressbar"
                aria-label="Move progress"
                aria-valuemin="0"
                aria-valuemax="100"
                aria-valuenow={progressView.percent}
              >
                <i style:width={`${progressView.percent}%`}></i>
              </div>
              <div class="prog-lbl">
                {progressView.label} · this number only goes up
              </div>
            </div>
          </div>
        </section>

        <section class="cell c-collide" aria-label="Collisions">
          <div class="ch">
            <span class="ic" aria-hidden="true">▲</span> Collisions
            <span class="badge">{collisionRows.length}</span>
          </div>
          <div class="collide-body">
            {#if collisionRows.length === 0}
              <p class="quiet-empty">No date collisions.</p>
            {:else}
              {#each collisionRows as collision}
                <div class="crow">
                  <span class="ic" aria-hidden="true"
                    >{collision.type === "task_span" ? "▲" : "△"}</span
                  >
                  <span class="d"
                    >{formatShortDate(collision.overlaps_from)}</span
                  >
                  <span><b>{collision.wording}</b></span>
                </div>
              {/each}
            {/if}
          </div>
        </section>

        <nav class="c-dock" aria-label="Details">
          <button
            class="dockbtn"
            type="button"
            onclick={() => openDialog(sellDialog)}
          >
            To sell <span class="v mono">{formatCad(sellTotal)}</span>
          </button>
          <button
            class="dockbtn"
            type="button"
            onclick={() => openDialog(calendarDialog)}
          >
            <span
              class:ok={milestones.some(
                (milestone) => milestone.gcal_state === "synced",
              )}
              class="dot"
              aria-hidden="true"
            ></span>
            Calendar <span class="v mono">{milestones.length} dates</span>
          </button>
          <button
            class="dockbtn"
            type="button"
            onclick={() => openDialog(rolesDialog)}
          >
            <span class:idle={roles.length === 0} class="dot" aria-hidden="true"
            ></span>
            Roles
            <span class="v"
              >{roles.length ? `${roles.length} active` : "dormant"}</span
            >
          </button>
          <button
            class="dockbtn"
            type="button"
            onclick={() => openDialog(tasksDialog)}
          >
            Tasks <span class="v mono">{openTasks.length} open</span>
          </button>
        </nav>

        <nav class="tabbar" aria-label="Sections">
          <button
            type="button"
            aria-pressed={mobileTab === "today"}
            onclick={() => (mobileTab = "today")}>Today</button
          >
          <button
            type="button"
            aria-pressed={mobileTab === "plan"}
            onclick={() => (mobileTab = "plan")}
          >
            Plan <span class="tb-n">▲ {collisionRows.length}</span>
          </button>
          <button
            type="button"
            aria-pressed={mobileTab === "more"}
            onclick={() => (mobileTab = "more")}>More</button
          >
        </nav>
      </div>
    </div>

    <dialog
      bind:this={tasksDialog}
      aria-labelledby="tasks-heading"
      onclick={closeFromBackdrop}
    >
      <div class="dlg-head">
        <h2 id="tasks-heading">What's left</h2>
        <span class="sub">{openTasks.length} open</span>
        <button
          class="x"
          type="button"
          aria-label="Close"
          onclick={() => tasksDialog.close()}>×</button
        >
      </div>
      <div class="dlg-body">
        {#if taskError}<p class="task-error" role="alert">{taskError}</p>{/if}
        {#if tasks.length === 0}
          <p class="prose dialog-empty">No tasks yet.</p>
        {:else}
          <div class="tsec">
            <div class="th">
              All tasks <span class="mono">{tasks.length}</span>
            </div>
            <ul class="tasks">
              {#each [...tasks].sort( (left, right) => (left.due_on ?? "9999").localeCompare(right.due_on ?? "9999"), ) as task (task.id)}
                {@const due = taskDueView(task.due_on, now)}
                <li class:done={task.done_at != null} class="task">
                  <input
                    id={`task-${task.id}`}
                    type="checkbox"
                    checked={task.done_at != null}
                    disabled={pendingTaskIds.has(task.id)}
                    onchange={() => toggleTask(task)}
                  />
                  <label class="t" for={`task-${task.id}`}>
                    <span class={`tdot ${taskHueClass(task)}`}></span>
                    <span>{task.title}</span>
                    {#if task.note}<span class="note">{task.note}</span>{/if}
                  </label>
                  <span class="who">{titleCaseName(task.owner)}</span>
                  <span
                    class:overdue={task.done_at == null &&
                      due.bucket === "overdue"}
                    class:soon={task.done_at == null && due.bucket === "week"}
                    class="due"
                    >{task.done_at == null
                      ? due.label
                      : formatTaskDueDate(task.due_on)}</span
                  >
                </li>
              {/each}
            </ul>
          </div>
        {/if}
      </div>
    </dialog>

    <dialog
      bind:this={sellDialog}
      aria-labelledby="sell-heading"
      onclick={closeFromBackdrop}
    >
      <div class="dlg-head">
        <h2 id="sell-heading">To sell</h2>
        <span class="sub">{formatCad(sellTotal)} total</span>
        <button
          class="x"
          type="button"
          aria-label="Close"
          onclick={() => sellDialog.close()}>×</button
        >
      </div>
      <div class="dlg-body">
        {#if taskError}<p class="task-error" role="alert">{taskError}</p>{/if}
        {#if sellTasks.length === 0}
          <p class="prose dialog-empty">Nothing is marked to sell.</p>
        {:else}
          <div class="tsec">
            <div class="th">
              Sale list <span class="mono">{sellTasks.length}</span>
            </div>
            <ul class="tasks sell-list">
              {#each sellTasks as task (task.id)}
                <li class:done={task.done_at != null} class="task">
                  <input
                    id={`sell-${task.id}`}
                    type="checkbox"
                    checked={task.done_at != null}
                    disabled={pendingTaskIds.has(task.id)}
                    onchange={() => toggleTask(task)}
                  />
                  <label class="t" for={`sell-${task.id}`}>
                    <span class="tdot hue-sell"></span><span>{task.title}</span>
                    {#if task.note}<span class="note">{task.note}</span>{/if}
                  </label>
                  <span class="who">{titleCaseName(task.owner)}</span>
                  <span class="due value">{formatCad(task.value_cad)}</span>
                </li>
              {/each}
            </ul>
          </div>
          <div class="sell-total">
            <span>Total</span><strong>{formatCad(sellTotal)}</strong>
          </div>
        {/if}
      </div>
    </dialog>

    <dialog
      bind:this={calendarDialog}
      aria-labelledby="calendar-heading"
      onclick={closeFromBackdrop}
    >
      <div class="dlg-head">
        <h2 id="calendar-heading">Shared calendar</h2>
        <span class="sub">{milestones.length} milestones</span>
        <button
          class="x"
          type="button"
          aria-label="Close"
          onclick={() => calendarDialog.close()}>×</button
        >
      </div>
      <div class="dlg-body">
        {#if milestones.length === 0}
          <p class="prose dialog-empty">No calendar milestones yet.</p>
        {:else}
          <ul class="cal">
            {#each milestones as milestone (milestone.id)}
              <li>
                <span class="d">{formatShortDate(milestone.occurs_on)}</span>
                <span>
                  {milestone.title}
                  <span class="cal-owner"
                    >· {titleCaseName(milestone.owner)}</span
                  >
                </span>
                <span class={`st ${calendarStateClass(milestone)}`}
                  >{calendarStateIcon(milestone)} {milestone.gcal_state}</span
                >
              </li>
            {/each}
          </ul>
        {/if}
      </div>
    </dialog>

    <dialog
      bind:this={rolesDialog}
      aria-labelledby="roles-heading"
      onclick={closeFromBackdrop}
    >
      <div class="dlg-head">
        <h2 id="roles-heading">Roles</h2>
        <span class="sub"
          >{roles.length ? `${roles.length} tracked` : "dormant"}</span
        >
        <button
          class="x"
          type="button"
          aria-label="Close"
          onclick={() => rolesDialog.close()}>×</button
        >
      </div>
      <div class="dlg-body">
        {#if roles.length === 0}
          <div class="roles-note">
            <span aria-hidden="true">○</span>
            <span
              ><strong>No roles are being tracked.</strong> This list can stay quiet
              until the search starts.</span
            >
          </div>
        {:else}
          {#each roles as role (role.id)}
            <div class="rrow">
              <span>
                {role.title}<span class="loc">{role.company}</span>
              </span>
              <span class="watch"
                >{role.stage ?? "watching"}{role.next_on
                  ? ` · ${formatShortDate(role.next_on)}`
                  : ""}</span
              >
            </div>
          {/each}
        {/if}
      </div>
    </dialog>
  </main>
{/if}
