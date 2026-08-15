<script>
  import { deserialize } from "$app/forms";
  import { launcher } from "$lib/private/launcher.js";
  import ClusterChatPanel from "$lib/private/components/ClusterChatPanel.svelte";
  import "$lib/private/dashboard-theme.css";
  import { periodForHour } from "$lib/private/period.js";

  let { data } = $props();

  // ── Dashboard data (60s client refresh) ──────
  // Every access below is null-safe: any section may be null or {error}
  // and SSR must never crash on missing data.
  // svelte-ignore state_referenced_locally
  let dash = $state(data.dashboard);

  $effect(() => {
    const id = setInterval(async () => {
      try {
        const res = await fetch("/dashboard-data");
        if (!res.ok) return;
        const json = await res.json();
        if (json && !json.error) dash = json;
      } catch {
        // keep the last good snapshot
      }
    }, 60_000);
    return () => clearInterval(id);
  });

  let events = $derived(
    dash?.today && !dash.today.error ? (dash.today.events ?? []) : null,
  );
  let health = $derived(
    dash?.health && !dash.health.error ? dash.health : null,
  );
  let alerts = $derived(
    dash?.alerts && !dash.alerts.error ? (dash.alerts.firing ?? []) : null,
  );
  let github = $derived(
    dash?.github && !dash.github.error ? dash.github : null,
  );
  let unhealthyKinds = $derived(
    health ? Object.entries(health.unhealthy ?? {}) : [],
  );
  let unhealthyCount = $derived(
    unhealthyKinds.reduce((n, [, rows]) => n + (rows?.length ?? 0), 0),
  );
  let allClear = $derived(
    health?.healthy === true && (alerts == null || alerts.length === 0),
  );

  // ── Schedule past/active logic ───────────────
  function timeToMinutes(timeStr) {
    const [h, m] = timeStr.split(":").map(Number);
    return h * 60 + m;
  }

  function nowMinutes(d) {
    return d.getHours() * 60 + d.getMinutes();
  }

  function isPast(ev, d) {
    if (ev.allDay) return false;
    const end = ev.endTime ?? ev.time;
    if (!end) return false;
    return nowMinutes(d) >= timeToMinutes(end);
  }

  function isActive(ev, d) {
    if (ev.allDay || !ev.endTime || !ev.time) return false;
    const n = nowMinutes(d);
    return n >= timeToMinutes(ev.time) && n < timeToMinutes(ev.endTime);
  }

  // ── Tasks ────────────────────────────────────
  const DONE_STATUSES = ["done", "cancelled"];

  // svelte-ignore state_referenced_locally
  let tasksDaily = $state(data.tasksDaily ?? []);
  // svelte-ignore state_referenced_locally
  let tasksWeekly = $state(data.tasksWeekly ?? []);

  // Weekly is a superset of daily (due this week vs due today/overdue);
  // show only the weekly items not already in the daily list.
  let dailyIds = $derived(new Set(tasksDaily.map((t) => t.note_id)));
  let weeklyOnly = $derived(
    tasksWeekly.filter((t) => !dailyIds.has(t.note_id)),
  );

  function isDone(task) {
    return DONE_STATUSES.includes(task?.status);
  }

  async function toggleTask(task) {
    const prev = task.status;
    // Optimistic flip; the server computes the same transition.
    task.status = DONE_STATUSES.includes(prev) ? "todo" : "done";
    try {
      const formData = new FormData();
      formData.append("note_id", task.note_id);
      formData.append("status", prev);
      const res = await fetch("?/toggleTask", {
        method: "POST",
        body: formData,
      });
      const outcome = deserialize(await res.text());
      if (outcome.type !== "success" || outcome.data?.error) throw new Error();
      if (outcome.data?.status) task.status = outcome.data.status;
    } catch {
      task.status = prev;
    }
  }

  // ── Clock ────────────────────────────────────
  let now = $state(new Date());
  $effect(() => {
    const id = setInterval(() => (now = new Date()), 60_000);
    return () => clearInterval(id);
  });

  function formatDate(d) {
    return d.toLocaleDateString("en-GB", {
      weekday: "long",
      day: "numeric",
      month: "long",
      year: "numeric",
    });
  }

  function formatTime(d) {
    return d.toLocaleTimeString("en-GB", {
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  function relTime(iso) {
    if (!iso) return "";
    const t = new Date(iso).getTime();
    if (Number.isNaN(t)) return "";
    const s = Math.round((now.getTime() - t) / 1000);
    if (s < 60) return "just now";
    const m = Math.round(s / 60);
    if (m < 60) return `${m}m ago`;
    const h = Math.round(m / 60);
    if (h < 24) return `${h}h ago`;
    const d = Math.round(h / 24);
    return `${d}d ago`;
  }

  // ── Ambient time-of-day theme ────────────────
  // The palette drifts with the clock: soft gold at dawn, airy by day,
  // rose at dusk, calm ink after dark. Same data, different light.
  let hour = $derived(now.getHours());
  let period = $derived(periodForHour(hour));
  let greeting = $derived(
    hour >= 5 && hour < 12
      ? "Good morning"
      : hour >= 12 && hour < 18
        ? "Good afternoon"
        : hour >= 18 && hour < 22
          ? "Good evening"
          : "Up late",
  );

  // Stable per-app hue for the launcher tiles.
  function hueFor(label) {
    let h = 0;
    for (const c of label) h = (h * 31 + c.charCodeAt(0)) % 360;
    return h;
  }
</script>

<svelte:head>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin="" />
  <link
    href="https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,300..700;1,9..144,300..700&family=Schibsted+Grotesk:ital,wght@0,400..900;1,400..900&display=swap"
    rel="stylesheet"
  />
</svelte:head>

<div class="shell {period}">
  <div class="dash">
    <header class="masthead">
      <div class="masthead-words">
        <h1 class="greeting">{greeting}<span class="greeting-mark">.</span></h1>
        <p class="masthead-date">{formatDate(now)}</p>
      </div>
      <time class="clock" datetime={now.toISOString()}>{formatTime(now)}</time>
    </header>

    <!-- Cluster pulse ribbon -->
    <section
      class="pulse"
      class:pulse--bad={health != null &&
        (!health.healthy || (alerts?.length ?? 0) > 0)}
    >
      {#if health == null && alerts == null}
        <span class="pulse-dot pulse-dot--unknown"></span>
        <span class="pulse-text">cluster status unavailable</span>
      {:else if allClear}
        <span class="pulse-dot pulse-dot--ok"></span>
        <span class="pulse-text">
          All quiet on the cluster
          <span class="pulse-sub"
            >{health?.scanned ?? 0} workloads scanned · no alerts firing</span
          >
        </span>
      {:else}
        <div class="pulse-head">
          <span class="pulse-dot pulse-dot--bad"></span>
          <span class="pulse-text">
            {#if health && !health.healthy}
              {unhealthyCount} unhealthy
              <span class="pulse-sub">
                {unhealthyKinds
                  .map(([kind, rows]) => `${rows?.length ?? 0} ${kind}`)
                  .join(" · ")}
              </span>
            {:else if health}
              workloads healthy
            {/if}
            {#if alerts && alerts.length > 0}
              <span class="pulse-sub"
                >{alerts.length} alert{alerts.length === 1 ? "" : "s"} firing</span
              >
            {/if}
          </span>
        </div>
        {#if health && !health.healthy}
          <ul class="pulse-list">
            {#each unhealthyKinds as [kind, rows]}
              {#each rows ?? [] as row}
                <li class="pulse-item">
                  <span class="pulse-kind">{kind}</span>
                  <span class="mono"
                    >{row.namespace ? `${row.namespace}/` : ""}{row.name}</span
                  >
                </li>
              {/each}
            {/each}
          </ul>
        {/if}
        {#if alerts && alerts.length > 0}
          <ul class="pulse-list">
            {#each alerts as alert}
              <li class="pulse-item pulse-item--alert">
                {alert.name}{alert.severity ? ` (${alert.severity})` : ""}
              </li>
            {/each}
          </ul>
        {/if}
      {/if}
    </section>

    <div class="grid">
      <!-- Today: events + tasks -->
      <section class="card card--today">
        <h2 class="section-label">Today</h2>
        {#if events == null}
          <p class="unavail">calendar unavailable</p>
        {:else if events.length === 0}
          <p class="unavail">a clear calendar</p>
        {:else}
          <ul class="event-list">
            {#each events as ev}
              <li
                class="event-row"
                class:event-row--past={isPast(ev, now)}
                class:event-row--active={isActive(ev, now)}
                class:event-row--allday={ev.allDay}
              >
                {#if ev.allDay}
                  <span class="event-time"></span>
                  <span class="event-title">{ev.title}</span>
                  <span class="event-meta">all day</span>
                {:else}
                  <span class="event-time">{ev.time}</span>
                  <span class="event-title">
                    {ev.title}
                    {#if ev.location}
                      <span class="event-location">{ev.location}</span>
                    {/if}
                  </span>
                  <span class="event-meta">{ev.endTime ? ev.endTime : ""}</span>
                {/if}
              </li>
            {/each}
          </ul>
        {/if}

        <h2 class="section-label section-label--stacked">Tasks</h2>
        {#if weeklyOnly.length === 0 && tasksDaily.length === 0}
          <p class="unavail">nothing due, go outside</p>
        {:else}
          <ul class="task-list">
            {#each weeklyOnly as task (task.note_id)}
              <li class="task-row">
                <button
                  class="task-check"
                  class:task-check--done={isDone(task)}
                  aria-pressed={isDone(task)}
                  aria-label={`Toggle ${task.title}`}
                  onclick={() => toggleTask(task)}
                >
                  <svg viewBox="0 0 12 12" aria-hidden="true">
                    <path
                      d="M2.5 6.5 5 9l4.5-6"
                      fill="none"
                      stroke-width="1.8"
                      stroke-linecap="round"
                      stroke-linejoin="round"
                    />
                  </svg>
                </button>
                <span
                  class="task-title task-title--weekly"
                  class:task-title--done={isDone(task)}>{task.title}</span
                >
                {#if task.due}
                  <span class="task-due">{task.due}</span>
                {/if}
              </li>
            {/each}
            {#each tasksDaily as task (task.note_id)}
              <li class="task-row">
                <button
                  class="task-check"
                  class:task-check--done={isDone(task)}
                  aria-pressed={isDone(task)}
                  aria-label={`Toggle ${task.title}`}
                  onclick={() => toggleTask(task)}
                >
                  <svg viewBox="0 0 12 12" aria-hidden="true">
                    <path
                      d="M2.5 6.5 5 9l4.5-6"
                      fill="none"
                      stroke-width="1.8"
                      stroke-linecap="round"
                      stroke-linejoin="round"
                    />
                  </svg>
                </button>
                <span class="task-title" class:task-title--done={isDone(task)}
                  >{task.title}</span
                >
                {#if task.due}
                  <span class="task-due">{task.due}</span>
                {/if}
              </li>
            {/each}
          </ul>
        {/if}
      </section>

      <!-- Shipping: PRs + merges -->
      <section class="card card--shipping">
        <h2 class="section-label">Shipping</h2>
        {#if github == null}
          <p class="unavail">unavailable</p>
        {:else}
          {#if (github.open_prs ?? []).length === 0}
            <p class="unavail">no open PRs</p>
          {:else}
            <ul class="plain-list">
              {#each github.open_prs ?? [] as pr}
                <li>
                  <a
                    class="pr-row"
                    href={pr.url}
                    target="_blank"
                    rel="noopener"
                  >
                    <span class="ci-dot ci-dot--{pr.ci ?? 'pending'}"></span>
                    <span class="pr-num mono">#{pr.number}</span>
                    <span class="pr-title">{pr.title}</span>
                    {#if pr.draft}
                      <span class="dim">draft</span>
                    {/if}
                  </a>
                </li>
              {/each}
            </ul>
          {/if}
          {#if (github.recent_merges ?? []).length > 0}
            <h3 class="sub-label">Merged</h3>
            <ul class="plain-list">
              {#each github.recent_merges ?? [] as pr}
                <li>
                  <a
                    class="pr-row pr-row--merged"
                    href={pr.url}
                    target="_blank"
                    rel="noopener"
                  >
                    <span class="pr-num mono">#{pr.number}</span>
                    <span class="pr-title">{pr.title}</span>
                    <span class="dim">{relTime(pr.merged_at)}</span>
                  </a>
                </li>
              {/each}
            </ul>
          {/if}
        {/if}
      </section>

      <!-- Launcher: internal cluster-ops tools -->
      <section class="launcher">
        <h2 class="section-label">Launcher</h2>
        <div class="launcher-grid">
          {#each launcher as item}
            <a
              href={item.href}
              class="tile"
              style="--tile-hue: {hueFor(item.label)}"
              target={item.external ? "_blank" : undefined}
              rel={item.external ? "noopener" : undefined}
            >
              <span class="tile-dot" aria-hidden="true"></span>
              <span class="tile-body">
                <span class="tile-name">
                  {item.label}
                  {#if item.external}
                    <span class="tile-ext" aria-hidden="true">↗</span>
                  {/if}
                </span>
                <span class="tile-desc">{item.desc}</span>
              </span>
            </a>
          {/each}
        </div>
      </section>

      <!-- Ask the cluster: full-width, display-dominant -->
      <section class="card card--chat">
        <h2 class="section-label">Ask the cluster</h2>
        <ClusterChatPanel />
      </section>
    </div>
  </div>
</div>

<style>
  /* ── Theme ─────────────────────────────────────
   * Everything is scoped to .shell: it owns its own scroll container,
   * a px-based type scale (immune to the global vw-driven rem base),
   * and the palette in $lib/private/dashboard-theme.css re-binds the
   * shared CSS variables (--fg, --border, --font…) so descendant
   * components (ClusterChatPanel) restyle without edits.
   */

  .shell {
    position: fixed;
    inset: 0;
    overflow-y: auto;
    font-family: var(--font-ui);
    font-size: 15px;
    line-height: 1.55;
    color: var(--ink);
    background:
      radial-gradient(1100px 700px at 12% -12%, var(--glow-a), transparent 62%),
      radial-gradient(900px 620px at 102% -6%, var(--glow-b), transparent 58%),
      var(--paper);
    -webkit-font-smoothing: antialiased;
  }

  /* ── Layout ────────────────────────────────── */

  .dash {
    max-width: 1360px;
    margin: 0 auto;
    padding: clamp(20px, 3.5vw, 52px) clamp(16px, 3vw, 44px) 72px;
  }

  .masthead {
    display: flex;
    justify-content: space-between;
    align-items: flex-end;
    gap: 24px;
    margin-bottom: clamp(20px, 3vw, 36px);
  }

  .greeting {
    font-family: var(--font-display);
    font-optical-sizing: auto;
    font-size: clamp(30px, 3.4vw, 46px);
    font-weight: 420;
    letter-spacing: -0.015em;
    line-height: 1.08;
    margin: 0;
  }

  .greeting-mark {
    color: var(--accent);
  }

  .masthead-date {
    margin: 6px 0 0 2px;
    font-size: 14px;
    color: var(--ink-2);
    letter-spacing: 0.01em;
  }

  .clock {
    font-family: var(--font-display);
    font-optical-sizing: auto;
    font-size: clamp(26px, 2.6vw, 38px);
    font-weight: 340;
    font-variant-numeric: tabular-nums;
    color: var(--ink-2);
    line-height: 1;
  }

  /* ── Cluster pulse ribbon ──────────────────── */

  .pulse {
    display: flex;
    flex-direction: column;
    gap: 8px;
    align-items: flex-start;
    padding: 12px 20px;
    margin-bottom: clamp(16px, 2vw, 24px);
    border: 1px solid var(--line);
    border-radius: 999px;
    background: color-mix(in srgb, var(--ok) 5%, var(--card-bg));
  }

  .pulse--bad {
    border-radius: var(--radius);
    background: color-mix(in srgb, var(--bad) 6%, var(--card-bg));
    border-color: color-mix(in srgb, var(--bad) 30%, var(--line));
  }

  .pulse-head {
    display: flex;
    align-items: center;
    gap: 10px;
  }

  .pulse:not(.pulse--bad) {
    flex-direction: row;
    align-items: center;
    gap: 10px;
  }

  .pulse-dot {
    width: 9px;
    height: 9px;
    border-radius: 50%;
    flex-shrink: 0;
  }

  .pulse-dot--ok {
    background: var(--ok);
  }

  .pulse-dot--bad {
    background: var(--bad);
  }

  .pulse-dot--unknown {
    background: var(--ink-3);
  }

  .pulse-text {
    font-size: 14px;
    font-weight: 600;
    letter-spacing: 0.005em;
  }

  .pulse-sub {
    font-weight: 450;
    color: var(--ink-2);
    margin-left: 10px;
    font-size: 13px;
  }

  .pulse-list {
    list-style: none;
    margin: 0;
    padding: 0 0 2px 19px;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .pulse-item {
    font-size: 13px;
    color: var(--ink-2);
  }

  .pulse-kind {
    color: var(--ink-3);
    font-size: 12px;
    margin-right: 6px;
  }

  .pulse-item--alert {
    color: var(--bad);
  }

  @media (prefers-reduced-motion: no-preference) {
    .pulse-dot--ok {
      animation: breathe 3.2s ease-in-out infinite;
    }

    @keyframes breathe {
      0%,
      100% {
        box-shadow: 0 0 0 0 color-mix(in srgb, var(--ok) 40%, transparent);
      }
      50% {
        box-shadow: 0 0 0 6px color-mix(in srgb, var(--ok) 0%, transparent);
      }
    }
  }

  /* ── Grid + cards ──────────────────────────── */

  .grid {
    display: grid;
    grid-template-columns: repeat(12, 1fr);
    gap: clamp(12px, 1.6vw, 20px);
  }

  .card {
    display: flex;
    flex-direction: column;
    gap: 10px;
    min-width: 0;
    padding: 20px 22px 22px;
    background: var(--card-bg);
    border: 1px solid var(--line);
    border-radius: var(--radius);
    box-shadow: 0 1px 2px rgba(20, 16, 8, 0.03);
  }

  .card--today {
    grid-column: span 5;
  }

  .card--shipping {
    grid-column: span 7;
  }

  .card--chat {
    grid-column: 1 / -1;
  }

  .launcher {
    grid-column: 1 / -1;
    display: flex;
    flex-direction: column;
    gap: 12px;
    margin-top: 10px;
  }

  @media (max-width: 900px) {
    .card--today,
    .card--shipping {
      grid-column: 1 / -1;
    }
  }

  @media (max-width: 700px) {
    .masthead {
      align-items: baseline;
    }
  }

  /* Staggered entrance */
  @media (prefers-reduced-motion: no-preference) {
    .masthead,
    .pulse,
    .card,
    .launcher {
      animation: rise 0.55s cubic-bezier(0.22, 1, 0.36, 1) both;
    }

    .pulse {
      animation-delay: 0.05s;
    }
    .card--today {
      animation-delay: 0.1s;
    }
    .card--shipping {
      animation-delay: 0.15s;
    }
    .launcher {
      animation-delay: 0.2s;
    }
    .card--chat {
      animation-delay: 0.25s;
    }

    @keyframes rise {
      from {
        opacity: 0;
        transform: translateY(14px);
      }
    }
  }

  .section-label {
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.16em;
    color: var(--ink-3);
    margin: 0 0 2px;
  }

  .section-label--stacked {
    margin-top: 14px;
    padding-top: 14px;
    border-top: 1px solid var(--line);
  }

  .sub-label {
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.16em;
    color: var(--ink-3);
    margin: 10px 0 0;
  }

  .unavail {
    font-size: 13px;
    color: var(--ink-3);
    margin: 0;
    font-style: italic;
  }

  .dim {
    color: var(--ink-3);
    font-size: 12px;
  }

  .mono {
    font-family: var(--font-code);
    font-size: 0.92em;
  }

  .plain-list {
    list-style: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  /* ── Schedule ──────────────────────────────── */

  .event-list {
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: 0;
    margin: 0;
  }

  .event-row {
    display: flex;
    align-items: baseline;
    gap: 12px;
    padding: 5px 0;
  }

  .event-time {
    font-size: 13px;
    color: var(--ink-2);
    font-variant-numeric: tabular-nums;
    min-width: 44px;
    flex-shrink: 0;
  }

  .event-title {
    font-size: 14px;
    flex: 1;
    min-width: 0;
  }

  .event-location {
    display: block;
    font-size: 12px;
    color: var(--ink-3);
  }

  .event-meta {
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--ink-3);
    flex-shrink: 0;
  }

  .event-row--active {
    font-weight: 650;
  }

  .event-row--active .event-time {
    color: var(--accent);
  }

  .event-row--past .event-time,
  .event-row--past .event-title,
  .event-row--past .event-meta {
    text-decoration: line-through;
    opacity: 0.35;
  }

  /* ── Tasks ─────────────────────────────────── */

  .task-list {
    list-style: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .task-row {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 4px 0;
  }

  .task-check {
    all: unset;
    box-sizing: border-box;
    width: 17px;
    height: 17px;
    border: 1.5px solid color-mix(in srgb, var(--ink) 32%, transparent);
    border-radius: 5px;
    cursor: pointer;
    flex-shrink: 0;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    transition:
      background 0.15s ease,
      border-color 0.15s ease;
  }

  .task-check svg {
    width: 11px;
    height: 11px;
    stroke: var(--card-bg);
    opacity: 0;
    transition: opacity 0.15s ease;
  }

  .task-check:hover {
    border-color: var(--accent);
  }

  .task-check--done {
    background: var(--accent);
    border-color: var(--accent);
  }

  .task-check--done svg {
    opacity: 1;
  }

  .task-check:focus-visible {
    outline: 2px solid color-mix(in srgb, var(--accent) 60%, transparent);
    outline-offset: 2px;
  }

  .task-title {
    font-size: 14px;
    flex: 1;
    min-width: 0;
    transition: opacity 0.2s ease;
  }

  .task-title--weekly {
    font-weight: 650;
  }

  .task-title--done {
    text-decoration: line-through;
    opacity: 0.35;
  }

  .task-due {
    font-size: 11px;
    color: var(--ink-3);
    font-variant-numeric: tabular-nums;
    flex-shrink: 0;
  }

  /* ── Shipping ──────────────────────────────── */

  .pr-row {
    display: flex;
    align-items: baseline;
    gap: 8px;
    padding: 4px 0;
    color: var(--ink-2);
    text-decoration: none;
    transition: color 0.15s ease;
    min-width: 0;
  }

  .pr-row:hover {
    color: var(--ink);
  }

  .pr-row:hover .pr-title {
    color: var(--accent);
  }

  .pr-num {
    font-size: 12px;
    color: var(--ink-3);
    font-variant-numeric: tabular-nums;
    flex-shrink: 0;
  }

  .pr-title {
    font-size: 13px;
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    transition: color 0.15s ease;
  }

  .ci-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
    align-self: center;
  }

  .ci-dot--passing {
    background: var(--ok);
  }

  .ci-dot--failing {
    background: var(--bad);
  }

  .ci-dot--pending {
    background: var(--ink-3);
    opacity: 0.45;
  }

  /* ── Launcher ──────────────────────────────── */

  .launcher-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(190px, 1fr));
    gap: 10px;
  }

  .tile {
    --tile-tint: oklch(0.62 0.11 var(--tile-hue));
    display: flex;
    align-items: flex-start;
    gap: 10px;
    padding: 13px 15px;
    background: var(--card-bg);
    border: 1px solid var(--line);
    border-radius: 14px;
    text-decoration: none;
    min-width: 0;
    transition:
      transform 0.18s cubic-bezier(0.22, 1, 0.36, 1),
      box-shadow 0.18s ease,
      border-color 0.18s ease;
  }

  .tile:hover {
    transform: translateY(-2px);
    border-color: color-mix(in srgb, var(--tile-tint) 45%, var(--line));
    box-shadow: 0 6px 18px -8px
      color-mix(in srgb, var(--tile-tint) 35%, rgba(20, 16, 8, 0.18));
  }

  .tile:focus-visible {
    outline: 2px solid color-mix(in srgb, var(--tile-tint) 70%, transparent);
    outline-offset: 2px;
  }

  .tile-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--tile-tint);
    margin-top: 7px;
    flex-shrink: 0;
    transition: transform 0.18s ease;
  }

  .tile:hover .tile-dot {
    transform: scale(1.35);
  }

  .tile-body {
    display: flex;
    flex-direction: column;
    gap: 1px;
    min-width: 0;
  }

  .tile-name {
    font-size: 14px;
    font-weight: 650;
    color: var(--ink);
    letter-spacing: -0.005em;
  }

  .tile-ext {
    font-size: 11px;
    color: var(--ink-3);
    display: inline-block;
    transition:
      transform 0.18s ease,
      color 0.18s ease;
  }

  .tile:hover .tile-ext {
    transform: translate(2px, -2px);
    color: var(--tile-tint);
  }

  .tile-desc {
    font-size: 12px;
    color: var(--ink-3);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
</style>
