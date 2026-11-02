<script>
  import { deserialize } from "$app/forms";
  import { launcher } from "$lib/private/launcher.js";
  import {
    ciState,
    mergeRead,
    missingReadings,
  } from "$lib/private/dashboard-read.js";
  import ClusterChatPanel from "$lib/private/components/ClusterChatPanel.svelte";
  import TechnicalDrawingChrome from "$lib/public/components/TechnicalDrawingChrome.svelte";
  import Trail from "$lib/public/components/Trail.svelte";
  import "$lib/public/styles/technical-drawing.css";
  import "./dashboard-sheet.css";

  let { data } = $props();

  // ── Dashboard data (60s client refresh) ──────
  // Every access below is null-safe: any section may be null or {error}
  // and SSR must never crash on missing data.
  // svelte-ignore state_referenced_locally
  let dash = $state(data.dashboard);

  // A refresh replaces the sections it brings and keeps the last good
  // reading of one it lacks for a couple of minutes (dashboard-read.js), so
  // a blip does not blank the page and a dead source still reads as dead.
  let misses = {};

  $effect(() => {
    const id = setInterval(async () => {
      if (document.visibilityState !== "visible") return;
      try {
        const res = await fetch("/dashboard-data");
        if (!res.ok) return;
        const merged = mergeRead(dash, await res.json(), misses);
        dash = merged.read;
        misses = merged.misses;
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
  let github = $derived(
    dash?.github && !dash.github.error ? dash.github : null,
  );
  let unhealthyKinds = $derived(
    health ? Object.entries(health.unhealthy ?? {}) : [],
  );
  let unhealthyCount = $derived(
    unhealthyKinds.reduce((n, [, rows]) => n + (rows?.length ?? 0), 0),
  );
  let allClear = $derived(health?.healthy === true);
  let factory = $derived(dash?.factory ?? null);
  let waiting = $derived(factory?.waiting ?? []);
  let failingPrs = $derived(
    (github?.open_prs ?? []).filter((pr) => ciState(pr.ci) === "bad"),
  );
  let missing = $derived(missingReadings(dash));

  // Everything that needs a hand from Joe, in the order it needs it:
  // decisions the factory stopped for, workloads the cluster reports
  // unhealthy, open PRs whose CI is red. One list, each row a place to go.
  let needs = $derived([
    ...waiting.map((item, index) => ({
      key: `decision:${item.receipt_id ?? item.work_item_id ?? `i${index}`}`,
      kind: "decision",
      href: "/factory/escalations",
      title:
        item.issue_number != null
          ? `#${item.issue_number} ${item.title}`
          : item.title || "untitled decision",
      detail: item.question,
    })),
    ...unhealthyKinds.flatMap(([kind, rows]) =>
      (rows ?? []).map((row) => ({
        key: `unhealthy:${kind}:${row.namespace}/${row.name}`,
        kind: "unhealthy",
        href: null,
        title: `${row.namespace ? `${row.namespace}/` : ""}${row.name}`,
        detail: kind,
      })),
    ),
    ...failingPrs.map((pr) => ({
      key: `ci:${pr.number}`,
      kind: "ci failed",
      href: pr.url,
      external: true,
      title: `#${pr.number} ${pr.title}`,
      detail: "",
    })),
  ]);

  // The factory cell of the status row: state and lane counts, or the
  // reason there is no reading. A missing summary is said, never zeroed.
  let factoryLine = $derived(
    factory == null
      ? "factory unavailable"
      : !factory.ok
        ? `factory ${factory.reason}`
        : `factory ${factory.state} · ${factory.inFlight} in flight · ${factory.queued} queued`,
  );
  // Green for an open lane, amber for a lane held (paused, draining), red
  // for a board that is not ready, grey for no reading.
  let factoryTone = $derived(
    factory == null
      ? "unknown"
      : !factory.ok
        ? "bad"
        : factory.state === "enabled"
          ? "ok"
          : "warn",
  );

  // The keypad: the registry's keys, with the live decision count and the
  // board's composed conductor link filled in when the factory answered.
  let keys = $derived(
    launcher.map((key) => ({
      ...key,
      href:
        key.dynamic === "conductor" && factory?.conductor?.href
          ? factory.conductor.href
          : key.href,
      count: key.count === "waiting" ? waiting.length : 0,
    })),
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

  // The clock still drives the greeting; the reader chooses the sheet scheme.
  let hour = $derived(now.getHours());
  let greeting = $derived(
    hour >= 5 && hour < 12
      ? "Good morning"
      : hour >= 12 && hour < 18
        ? "Good afternoon"
        : hour >= 18 && hour < 22
          ? "Good evening"
          : "Up late",
  );
</script>

<svelte:head>
  <title>dashboard · private.jomcgi.dev</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin="" />
  <link
    href="https://fonts.googleapis.com/css2?family=Schibsted+Grotesk:ital,wght@0,400..900;1,400..900&display=swap"
    rel="stylesheet"
  />
</svelte:head>

{#snippet trail()}
  <Trail root={{ label: "dashboard", href: "/" }} />
{/snippet}

<TechnicalDrawingChrome {trail} />
<main class="td sheet dashboard">
  <div class="frame">
    <div class="journal single">
      <div class="content">
        <div class="trail-inline dashboard-trail">{@render trail()}</div>

        <header class="masthead">
          <div class="masthead-words">
            <h1 class="greeting">
              {greeting}<span class="greeting-mark">.</span>
            </h1>
            <p class="masthead-date">{formatDate(now)}</p>
          </div>
          <time class="clock" datetime={now.toISOString()}
            >{formatTime(now)}</time
          >
        </header>

        <!-- What needs a hand, then the two readings that say why or why not. -->
        <section class="edition needs" aria-labelledby="needs-h">
          <h2 class="ed-head" id="needs-h">
            Needs you <span class="num">{needs.length}</span>
          </h2>
          {#if needs.length === 0}
            <p class="none">
              {dash == null
                ? "No reading. The dashboard is unavailable."
                : missing.length
                  ? `Nothing waiting in what answered. No reading from ${missing.join(", ")}.`
                  : "Nothing is waiting on you."}
            </p>
          {:else}
            <dl class="spec needs-list">
              {#each needs as item (item.key)}
                <div class="row">
                  <dt>{item.kind}</dt>
                  <dd>
                    {#if item.href}
                      <a
                        class="need"
                        href={item.href}
                        target={item.external ? "_blank" : undefined}
                        rel={item.external ? "noopener" : undefined}
                        >{item.title}</a
                      >
                    {:else}
                      <span class="need mono">{item.title}</span>
                    {/if}
                    {#if item.detail}
                      <span class="need-detail">{item.detail}</span>
                    {/if}
                  </dd>
                </div>
              {/each}
            </dl>
          {/if}
          <ul
            class="divided pulse"
            class:pulse--bad={health != null && !health.healthy}
            aria-label="Readings"
          >
            <li class="reading">
              {#if health == null}
                <span class="pulse-dot pulse-dot--unknown"></span>
                cluster status unavailable
              {:else if allClear}
                <span class="pulse-dot pulse-dot--ok"></span>
                all quiet · {health.scanned ?? 0} workloads scanned
              {:else}
                <span class="pulse-dot pulse-dot--bad"></span>
                {unhealthyCount} unhealthy
                <span class="pulse-sub"
                  >{unhealthyKinds
                    .map(([kind, rows]) => `${rows?.length ?? 0} ${kind}`)
                    .join(" · ")}</span
                >
              {/if}
            </li>
            <li class="reading">
              <a href="/factory">
                <span class="pulse-dot pulse-dot--{factoryTone}"></span>
                {factoryLine}
              </a>
            </li>
          </ul>
        </section>

        <!-- Every private tool, one key each. The count is the decisions key. -->
        <nav class="edition keypad" aria-label="Tools">
          <ul class="divided keys">
            {#each keys as key (key.label)}
              <li>
                <a
                  class="key"
                  href={key.href}
                  target={key.external ? "_blank" : undefined}
                  rel={key.external ? "noopener" : undefined}
                >
                  {key.label}
                  {#if key.count}
                    <span class="num">{key.count}</span>
                  {/if}
                  {#if key.external}
                    <span class="ext" aria-hidden="true">↗</span>
                  {/if}
                </a>
              </li>
            {/each}
          </ul>
        </nav>

        <div class="pair">
          <!-- Today: events + tasks -->
          <section class="edition card card--today" aria-labelledby="today-h">
            <h2 class="ed-head" id="today-h">Today</h2>
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
                      <span class="event-meta"
                        >{ev.endTime ? ev.endTime : ""}</span
                      >
                    {/if}
                  </li>
                {/each}
              </ul>
            {/if}

            <h2 class="ed-head ed-head--stacked">Tasks</h2>
            {#if weeklyOnly.length === 0 && tasksDaily.length === 0}
              <p class="unavail">nothing due, go outside</p>
            {:else}
              <ul class="task-list">
                {#each weeklyOnly as task (task.note_id)}
                  {@render taskRow(task, true)}
                {/each}
                {#each tasksDaily as task (task.note_id)}
                  {@render taskRow(task, false)}
                {/each}
              </ul>
            {/if}
          </section>

          <!-- Shipping: PRs + merges -->
          <section
            class="edition card card--shipping"
            aria-labelledby="shipping-h"
          >
            <h2 class="ed-head" id="shipping-h">Shipping</h2>
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
                        <span class="ci-dot ci-dot--{ciState(pr.ci)}"></span>
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
        </div>

        <!-- Ask the cluster: the conversational entry, keyed from the keypad. -->
        <section
          class="edition card card--chat"
          id="ask"
          aria-labelledby="ask-h"
        >
          <h2 class="ed-head" id="ask-h">Ask the cluster</h2>
          <ClusterChatPanel />
        </section>
      </div>
    </div>
  </div>
</main>

{#snippet taskRow(task, weekly)}
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
      class="task-title"
      class:task-title--weekly={weekly}
      class:task-title--done={isDone(task)}>{task.title}</span
    >
    {#if task.due}
      <span class="task-due">{task.due}</span>
    {/if}
  </li>
{/snippet}
