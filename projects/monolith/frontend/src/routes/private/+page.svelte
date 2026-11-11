<script>
  import { untrack } from "svelte";
  import { pushState, replaceState } from "$app/navigation";
  import { page } from "$app/state";
  import { deserialize } from "$app/forms";
  import { launcher } from "$lib/private/launcher.js";
  import {
    ciState,
    mergeRead,
    missingReadings,
  } from "$lib/private/dashboard-read.js";
  import ClusterChatPanel from "$lib/private/components/ClusterChatPanel.svelte";
  import DecisionCard from "$lib/private/components/DecisionCard.svelte";
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
  // A failed refresh is a miss for every section. One refresh runs at a
  // time, each is bounded by REFRESH_TIMEOUT_MS so a stalled request cannot
  // hold the next one forever, and none starts within MIN_GAP_MS of the
  // last attempt, so misses stay spaced in time and switching apps during a
  // short restart cannot age the page out in seconds.
  const MIN_GAP_MS = 30_000;
  const REFRESH_TIMEOUT_MS = 20_000;
  let misses = {};
  let inFlight = false;
  // Monotonic, so a wall clock stepping backwards cannot hold refreshes.
  let lastAttempt = performance.now();

  let destroyed = false;

  async function refresh({ soon = false } = {}) {
    if (inFlight || destroyed) return;
    if (!soon && performance.now() - lastAttempt < MIN_GAP_MS) return;
    inFlight = true;
    lastAttempt = performance.now();
    let next = null;
    try {
      const res = await fetch("/dashboard-data", {
        signal: AbortSignal.timeout(REFRESH_TIMEOUT_MS),
      });
      if (res.ok) next = await res.json();
    } catch {
      // no read this tick
    } finally {
      inFlight = false;
    }
    if (destroyed) return;
    if (soon) {
      // A quick follow-up only asks after the board: it never counts a miss
      // against the other readings, which keep their minute cadence.
      if (next?.factory != null) {
        dash = { ...dash, factory: next.factory };
        delete dash.factory_pending;
      } else if (next && !next.factory_pending) {
        dash = { ...dash };
        delete dash.factory_pending;
      }
    } else {
      const merged = mergeRead(dash, next, misses);
      dash = merged.read;
      misses = merged.misses;
    }
    followUp();
  }

  // A board read on its way lands in seconds, not a minute: ask again soon,
  // a few times, rather than showing "reading" until the next tick.
  const SOON_MS = 5_000;
  const SOON_TRIES = 6;
  let soonTries = 0;
  let soonTimer = null;
  function followUp() {
    clearTimeout(soonTimer);
    if (destroyed) return;
    if (!dash?.factory_pending || soonTries >= SOON_TRIES) {
      if (!dash?.factory_pending) soonTries = 0;
      return;
    }
    soonTries += 1;
    soonTimer = setTimeout(() => {
      if (document.visibilityState === "visible") refresh({ soon: true });
    }, SOON_MS);
  }

  $effect(() => {
    const id = setInterval(() => {
      if (document.visibilityState === "visible") refresh();
    }, 60_000);
    // Reopening the installed app or the tab reads now rather than up to a
    // minute later (hidden ticks are skipped above), unless it just read.
    const onVisible = () => {
      if (document.visibilityState === "visible") refresh();
    };
    document.addEventListener("visibilitychange", onVisible);
    untrack(followUp);
    return () => {
      destroyed = true;
      clearInterval(id);
      clearTimeout(soonTimer);
      document.removeEventListener("visibilitychange", onVisible);
    };
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
  // Decisions answered here leave the list at once; the board drops them on
  // its next read (the answer invalidates the server's cached board).
  let answered = $state(new Set());
  let decisions = $derived(
    waiting.filter(
      (item) =>
        item.decision_id &&
        !answered.has(`${item.receipt_id}:${item.decision_id}`),
    ),
  );
  // The deck: one decision in front, the rest queued under it. Tapping a
  // queued one brings it forward; answering the front one brings up the next.
  let front = $state(null);
  let deck = $derived.by(() => {
    const keyOf = (item) => `${item.receipt_id}:${item.decision_id}`;
    const first = decisions.find((item) => keyOf(item) === front);
    return first
      ? [first, ...decisions.filter((item) => keyOf(item) !== front)]
      : decisions;
  });
  let lastAnswer = $state("");
  function decided(item, out) {
    answered = new Set([...answered, `${item.receipt_id}:${item.decision_id}`]);
    lastAnswer = out.message;
  }

  // Everything else that needs a hand: workloads the cluster reports
  // unhealthy, open PRs whose CI is red. One list, each row a place to go.
  let needs = $derived([
    ...unhealthyKinds.flatMap(([kind, rows]) =>
      (rows ?? []).map((row) => {
        const name = `${row.namespace ? `${row.namespace}/` : ""}${row.name}`;
        return {
          key: `unhealthy:${kind}:${name}`,
          tag: singular(kind),
          title: name,
          // No page of its own: ask the cluster agent why, in the Ask sheet.
          ask: `Why is the ${singular(kind)} ${name} unhealthy, and what would fix it?`,
        };
      }),
    ),
    ...failingPrs.map((pr) => ({
      key: `ci:${pr.number}`,
      tag: "ci failed",
      title: `#${pr.number} ${pr.title}`,
      href: pr.url,
    })),
  ]);

  // "applications" -> "application": the health rollup names kinds plurally.
  function singular(kind) {
    return kind.endsWith("s") ? kind.slice(0, -1) : kind;
  }

  let askSeed = $state(null);
  function askAbout(item) {
    askSeed = { text: item.ask, at: Date.now() };
    openSheet("ask");
  }

  // The factory cell of the status row: state and lane counts, or the
  // reason there is no reading. A missing summary is said, never zeroed.
  let factoryLine = $derived(
    factory == null
      ? dash?.factory_pending
        ? "factory reading…"
        : "factory unavailable"
      : !factory.ok
        ? `factory ${factory.reason}`
        : `factory ${factory.state} · ${factory.inFlight} in flight · ${factory.queued} queued${age(factory.readAgoMs)}`,
  );
  // A board kept from a while ago says so; a fresh one says nothing. The
  // prewarm reads every five minutes, so past six the reading is old.
  const OLD_BOARD_MS = 6 * 60_000;
  function age(ms) {
    if (!(ms >= OLD_BOARD_MS)) return "";
    return ` · ${Math.round(ms / 60_000)} min ago`;
  }
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

  // ── Sheets (phone) ────────────────────────────
  // On a phone the page is one screen: what needs Joe, a line each for
  // today and shipping, and an ask bar in thumb reach. Each opens its panel
  // as a bottom sheet that scrolls inside itself and closes by swipe down,
  // tap outside, Esc or the system back gesture (each open pushes one
  // history entry). On a wide screen the same panels lay out inline.
  let phone = $state(false);
  // The open sheet lives in SvelteKit's shallow-routing state, so the back
  // gesture closes it and a link followed from inside it comes back to it.
  let sheet = $derived(phone ? (page.state.sheet ?? null) : null);
  let dragY = $state(0);
  let dragFrom = null;
  // What opened each sheet, so focus goes back there when it closes.
  const openers = {};

  function openSheet(id) {
    if (!phone) {
      document.getElementById(`panel-${id}`)?.scrollIntoView();
      return;
    }
    if (sheet === id) return;
    dragY = 0;
    if (sheet) replaceState("", { sheet: id });
    else pushState("", { sheet: id });
  }

  // One back per open sheet: a held Escape or a drag release followed by a
  // tap could otherwise step back past the dashboard before popstate lands.
  let closing = null;
  function closeSheet() {
    if (!sheet || closing === sheet) return;
    closing = sheet;
    dragY = 0;
    history.back();
  }

  // Focus follows the sheet in, and back to its dock key on the way out.
  let lastSheet = null;
  $effect(() => {
    const now = sheet;
    closing = null;
    if (now) {
      // Ask opens to its question box; every other sheet to its Done key.
      const target =
        now === "ask"
          ? document.querySelector("#panel-ask input")
          : document.getElementById(`close-${now}`);
      target?.focus({ preventScroll: true });
    } else if (lastSheet && phone) {
      openers[lastSheet]?.focus();
    }
    lastSheet = now;
  });

  function grab(event) {
    // Done sits in the grab strip; a captured pointer would steal its click.
    if (event.target.closest("button")) return;
    dragFrom = event.clientY;
    event.currentTarget.setPointerCapture?.(event.pointerId);
  }
  function drag(event) {
    if (dragFrom != null) dragY = Math.max(0, event.clientY - dragFrom);
  }
  function release() {
    if (dragFrom == null) return;
    dragFrom = null;
    if (dragY > 90) closeSheet();
    else dragY = 0;
  }

  $effect(() => {
    const media = window.matchMedia("(max-width: 760px)");
    const sync = () => {
      const narrow = media.matches;
      phone = narrow;
      // A sheet open when the window widens would leave a dead back entry.
      if (!narrow && untrack(() => page.state.sheet)) history.back();
    };
    sync();
    media.addEventListener("change", sync);
    const onKey = (event) => {
      if (event.key !== "Escape" || event.isComposing || event.defaultPrevented)
        return;
      if (untrack(() => sheet)) closeSheet();
    };
    window.addEventListener("keydown", onKey);
    return () => {
      media.removeEventListener("change", sync);
      window.removeEventListener("keydown", onKey);
    };
  });

  // What each dock key carries: the count that makes it worth opening.
  // The glance lines: the next thing on the calendar and the open tasks,
  // and what is shipping. Each opens its sheet.
  let openTasks = $derived(
    [...weeklyOnly, ...tasksDaily].filter((task) => !isDone(task)).length,
  );
  let nextEvent = $derived(
    (events ?? []).find((ev) => !ev.allDay && !isPast(ev, now)) ??
      (events ?? []).find((ev) => ev.allDay) ??
      null,
  );
  let todayLine = $derived(
    [
      events == null
        ? "calendar unavailable"
        : nextEvent == null
          ? "clear calendar"
          : nextEvent.allDay
            ? nextEvent.title
            : `${nextEvent.time} ${nextEvent.title}`,
      openTasks ? `${openTasks} task${openTasks === 1 ? "" : "s"}` : "no tasks",
    ].join(" · "),
  );
  let shippingLine = $derived.by(() => {
    if (github == null) return "github unavailable";
    const open = (github.open_prs ?? []).length;
    if (!open) return "nothing open";
    return `${open} open${failingPrs.length ? ` · ${failingPrs.length} failing` : ""}`;
  });

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

  // A refused toggle reverts the tick; on a phone that reads as a missed
  // tap unless the page says it failed.
  let taskError = $state("");

  async function toggleTask(task) {
    const prev = task.status;
    taskError = "";
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
      taskError = `Could not update ${task.title}.`;
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
<main class="td sheet dashboard" class:sheet-open={sheet != null}>
  <div class="frame">
    <div class="journal single">
      <div class="content">
        <div class="trail-inline dashboard-trail" inert={sheet != null}>
          {@render trail()}
        </div>

        <header class="masthead" inert={sheet != null}>
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

        <!-- The two readings: is the cluster quiet, is the factory moving. -->
        <ul
          class="divided pulse"
          inert={sheet != null}
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
              <span
                >{unhealthyCount} unhealthy{#if health.scanned > unhealthyCount}<span
                    class="pulse-sub"
                    >{` · ${health.scanned - unhealthyCount} healthy`}</span
                  >{/if}</span
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

        <!-- What needs a hand: the decision deck, then anything else red. -->
        <section
          class="edition needs"
          aria-labelledby="needs-h"
          inert={sheet != null}
        >
          <h2 class="ed-head" id="needs-h">
            Needs you <span class="num">{decisions.length + needs.length}</span>
          </h2>
          {#if lastAnswer}
            <p class="answered" role="status">{lastAnswer}</p>
          {/if}
          {#if deck.length}
            {#key `${deck[0].receipt_id}:${deck[0].decision_id}`}
              <div class="deck-front">
                <DecisionCard item={deck[0]} ondecided={decided} />
              </div>
            {/key}
            {#if deck.length > 1}
              <ul class="queue" aria-label="Next decisions">
                {#each deck.slice(1) as item (`${item.receipt_id}:${item.decision_id}`)}
                  <li>
                    <button
                      type="button"
                      class="queued"
                      onclick={() =>
                        (front = `${item.receipt_id}:${item.decision_id}`)}
                    >
                      <span class="queued-label">next</span>
                      <span class="queued-title"
                        >#{item.issue_number} {item.title}</span
                      >
                    </button>
                  </li>
                {/each}
              </ul>
            {/if}
          {/if}
          {#if decisions.length === 0 && needs.length === 0}
            <p class="none">
              {dash == null
                ? "No reading. The dashboard is unavailable."
                : missing.length
                  ? `Nothing waiting in what answered. No reading from ${missing.join(", ")}.`
                  : dash.factory_pending
                    ? "Nothing else waiting. Reading the factory board…"
                    : factory?.readAgoMs >= OLD_BOARD_MS
                      ? `Nothing was waiting ${Math.round(factory.readAgoMs / 60_000)} min ago.`
                      : "Nothing is waiting on you."}
            </p>
          {:else if needs.length}
            <ul class="needs-list" aria-label="Also needs you">
              {#each needs as item (item.key)}
                <li>
                  {#if item.href}
                    <a
                      class="need"
                      href={item.href}
                      target="_blank"
                      rel="noopener"
                    >
                      <span class="need-mark" aria-hidden="true"></span>
                      <span class="need-title">{item.title}</span>
                      <span class="need-tag">{item.tag} ↗</span>
                    </a>
                  {:else}
                    <button
                      type="button"
                      class="need"
                      onclick={() => askAbout(item)}
                    >
                      <span class="need-mark" aria-hidden="true"></span>
                      <span class="need-title mono">{item.title}</span>
                      <span class="need-tag">{item.tag} · ask</span>
                    </button>
                  {/if}
                </li>
              {/each}
            </ul>
          {/if}
        </section>

        <!-- A line each for today and shipping; each opens its sheet. -->
        <nav
          class="glance"
          aria-label="Today and shipping"
          inert={sheet != null}
        >
          <button
            type="button"
            class="glance-row"
            aria-controls="panel-today"
            aria-expanded={sheet === "today"}
            bind:this={openers.today}
            onclick={() => openSheet("today")}
          >
            <span class="glance-label">Today</span>
            <span class="glance-line">{todayLine}</span>
          </button>
          <button
            type="button"
            class="glance-row"
            aria-controls="panel-shipping"
            aria-expanded={sheet === "shipping"}
            bind:this={openers.shipping}
            onclick={() => openSheet("shipping")}
          >
            <span class="glance-label">Shipping</span>
            <span class="glance-line">{shippingLine}</span>
          </button>
        </nav>

        <button
          type="button"
          class="scrim"
          tabindex="-1"
          aria-label="Close"
          onclick={closeSheet}
        ></button>

        <div class="panels">
          {@render panel("tools", "Tools", toolsBody)}
          {@render panel("today", "Today", todayBody)}
          {@render panel("shipping", "Shipping", shippingBody)}
          {@render panel("ask", "Ask the cluster", askBody)}
        </div>
      </div>
    </div>
  </div>

  <nav class="dock" aria-label="Ask and tools" inert={sheet != null}>
    <button
      type="button"
      class="ask-bar"
      aria-controls="panel-ask"
      aria-expanded={sheet === "ask"}
      bind:this={openers.ask}
      onclick={() => openSheet("ask")}
    >
      <span>Ask the cluster…</span>
      <span aria-hidden="true">→</span>
    </button>
    <button
      type="button"
      class="dock-key"
      aria-controls="panel-tools"
      aria-expanded={sheet === "tools"}
      bind:this={openers.tools}
      onclick={() => openSheet("tools")}>Tools</button
    >
  </nav>
</main>

{#snippet panel(id, title, body)}
  {@const open = sheet === id}
  <section
    id="panel-{id}"
    class="edition card panel panel--{id}"
    class:open
    class:dragging={dragY > 0}
    style:--drag="{open ? dragY : 0}px"
    role={phone ? "dialog" : undefined}
    aria-modal={phone && open ? "true" : undefined}
    aria-labelledby="{id}-h"
    inert={phone && !open}
  >
    <div
      class="grab"
      onpointerdown={grab}
      onpointermove={drag}
      onpointerup={release}
      onpointercancel={release}
    >
      <span class="grab-bar" aria-hidden="true"></span>
      <h2 class="ed-head" id="{id}-h">{title}</h2>
      <button
        type="button"
        class="close"
        id="close-{id}"
        aria-label="Close {title}"
        onclick={closeSheet}>Done</button
      >
    </div>
    <div class="panel-body">{@render body()}</div>
  </section>
{/snippet}

{#snippet toolsBody()}
  <ul class="divided keys">
    {#each keys as key (key.label)}
      <li>
        {#if key.href === "#ask"}
          <button type="button" class="key" onclick={() => openSheet("ask")}
            >{key.label}</button
          >
        {:else}
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
        {/if}
      </li>
    {/each}
  </ul>
{/snippet}

{#snippet todayBody()}
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

  <h3 class="sub-label sub-label--tasks">Tasks</h3>
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
  {#if taskError}
    <p class="unavail task-error" role="alert">{taskError}</p>
  {/if}
{/snippet}

{#snippet shippingBody()}
  {#if github == null}
    <p class="unavail">unavailable</p>
  {:else}
    {#if (github.open_prs ?? []).length === 0}
      <p class="unavail">no open PRs</p>
    {:else}
      <ul class="plain-list">
        {#each github.open_prs ?? [] as pr}
          <li>
            <a class="pr-row" href={pr.url} target="_blank" rel="noopener">
              <span class="ci-dot ci-dot--{ciState(pr.ci)}" aria-hidden="true"
              ></span>
              <span class="sr-only">ci {pr.ci ?? "pending"}</span>
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
{/snippet}

{#snippet askBody()}
  <ClusterChatPanel seed={askSeed} />
{/snippet}

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
