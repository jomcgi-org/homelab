<script>
  import { tick } from "svelte";
  import { deserialize } from "$app/forms";
  import { launcher } from "$lib/private/launcher.js";
  import ClusterChatPanel from "$lib/private/components/ClusterChatPanel.svelte";

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
  let health = $derived(dash?.health && !dash.health.error ? dash.health : null);
  let alerts = $derived(
    dash?.alerts && !dash.alerts.error ? (dash.alerts.firing ?? []) : null,
  );
  let github = $derived(
    dash?.github && !dash.github.error ? dash.github : null,
  );
  let queues = $derived(
    dash?.queues && !dash.queues.error ? dash.queues : null,
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

  // ── Capture ──────────────────────────────────
  let note = $state("");
  let sent = $state(false);
  let captureRef = $state(null);
  let ingestMode = $state(false);

  $effect(() => {
    captureRef?.focus();
  });

  let error = $state(false);

  // ── Knowledge search overlay ─────────────────
  let searchOpen = $state(false);
  let searchQuery = $state("");
  let searchResults = $state([]);
  let selectedNote = $state(null);
  let activeIndex = $state(-1);
  let searching = $state(false);
  let searchError = $state("");
  let searchType = $state("all");
  let savedCapture = $state("");
  let searchInputRef = $state(null);

  function openSearch() {
    savedCapture = note;
    searchOpen = true;
    tick().then(() => searchInputRef?.focus());
  }

  function closeSearch() {
    searchOpen = false;
    note = savedCapture;
    searchQuery = "";
    searchResults = [];
    selectedNote = null;
    activeIndex = -1;
    searching = false;
    searchError = "";
    tick().then(() => captureRef?.focus());
  }

  function slugify(s) {
    return s
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-|-$/g, "");
  }

  function renderNote(md) {
    const blocks = md.replace(/^---\n[\s\S]*?\n---\n?/, "").split(/\n\n+/);
    return blocks.map((block) => {
      const h = block.match(/^(#{1,3}) (.+)$/m);
      if (h) return { tag: `h${h[1].length}`, id: slugify(h[2]), text: h[2] };
      return { tag: "p", text: block };
    });
  }

  async function selectResult(result) {
    try {
      const formData = new FormData();
      formData.append("note_id", result.note_id);
      const res = await fetch("?/preview", {
        method: "POST",
        body: formData,
      });
      const outcome = deserialize(await res.text());
      if (outcome.type === "success" && outcome.data?.note) {
        selectedNote = { ...outcome.data.note, section: result.section };
      }
    } catch (e) {
      console.error("Failed to fetch note:", e);
    }
  }

  $effect(() => {
    if (selectedNote?.content && selectedNote?.section) {
      tick().then(() => {
        const slug = slugify(selectedNote.section.replace(/^#+\s*/, ""));
        document.getElementById(slug)?.scrollIntoView({ block: "start" });
      });
    }
  });

  $effect(() => {
    function handleGlobalKeyDown(e) {
      if ((e.metaKey || e.ctrlKey) && e.key === "i") {
        e.preventDefault();
        ingestMode = !ingestMode;
        note = "";
        tick().then(() => captureRef?.focus());
        return;
      }
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        if (!searchOpen) openSearch();
      }
      if (e.key === "Escape" && searchOpen) {
        e.preventDefault();
        closeSearch();
      }
      if (searchOpen && e.key === "ArrowLeft" && selectedNote) {
        e.preventDefault();
        selectedNote = null;
      }
      if (searchOpen && e.key === "ArrowDown" && !selectedNote && searchResults.length > 0) {
        e.preventDefault();
        activeIndex = Math.min(activeIndex + 1, searchResults.length - 1);
      }
      if (searchOpen && e.key === "ArrowUp" && !selectedNote && searchResults.length > 0) {
        e.preventDefault();
        activeIndex = Math.max(activeIndex - 1, -1);
      }
      if (searchOpen && e.key === "Enter" && !selectedNote && activeIndex >= 0) {
        e.preventDefault();
        const result = searchResults[activeIndex];
        if (result) selectResult(result);
      }
    }
    document.addEventListener("keydown", handleGlobalKeyDown);
    return () => document.removeEventListener("keydown", handleGlobalKeyDown);
  });

  // ── Debounced search ───────────────────────────
  let searchTimer;
  let searchController;
  $effect(() => {
    clearTimeout(searchTimer);
    searchController?.abort();
    const q = searchQuery;
    const type = searchType;
    if (q.length < 2) {
      searchResults = [];
      searching = false;
      return;
    }
    searching = true;
    searchError = "";
    searchTimer = setTimeout(async () => {
      const controller = new AbortController();
      searchController = controller;
      try {
        const formData = new FormData();
        formData.append("q", q);
        if (type !== "all") formData.append("type", type);
        const res = await fetch("?/search", {
          method: "POST",
          body: formData,
          signal: controller.signal,
        });
        if (controller.signal.aborted) return;
        const outcome = deserialize(await res.text());
        if (outcome.type === "success") {
          const d = outcome.data;
          if (d.error) {
            searchError = d.error;
            searchResults = [];
          } else {
            searchResults = d.results;
            activeIndex = -1;
            searchError = "";
          }
        } else {
          searchError = "search failed";
          searchResults = [];
        }
      } catch (e) {
        if (e.name !== "AbortError") {
          searchError = "search unavailable";
          searchResults = [];
        }
      } finally {
        if (!controller.signal.aborted) {
          searching = false;
        }
      }
    }, 300);
    return () => {
      clearTimeout(searchTimer);
      searchController?.abort();
    };
  });

  async function submitCapture() {
    if (!note.trim()) return;
    try {
      const formData = new FormData();
      formData.append("content", note);
      const res = await fetch("?/capture", {
        method: "POST",
        body: formData,
      });
      if (!res.ok) throw new Error();
      sent = true;
      setTimeout(() => {
        note = "";
        sent = false;
        captureRef?.focus();
      }, 500);
    } catch {
      error = true;
      setTimeout(() => {
        error = false;
        captureRef?.focus();
      }, 2000);
    }
  }

  async function submitIngest() {
    if (!note.trim()) return;
    try {
      const formData = new FormData();
      formData.append("url", note.trim());
      formData.append("source_type", detectSourceType(note.trim()));
      const res = await fetch("?/ingest", {
        method: "POST",
        body: formData,
      });
      if (!res.ok) throw new Error();
      sent = true;
      setTimeout(() => {
        note = "";
        sent = false;
        ingestMode = false;
        captureRef?.focus();
      }, 500);
    } catch {
      error = true;
      setTimeout(() => {
        error = false;
        captureRef?.focus();
      }, 2000);
    }
  }

  function detectSourceType(url) {
    if (/youtube\.com|youtu\.be/.test(url)) return "youtube";
    return "webpage";
  }

  function captureKeyDown(e) {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
      e.preventDefault();
      ingestMode ? submitIngest() : submitCapture();
    }
  }

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
      weekday: "short",
      day: "numeric",
      month: "short",
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
</script>

<div class="dash">
  <header class="dash-header">
    <span class="date">{formatDate(now)}</span>
    <span class="clock">{formatTime(now)}</span>
  </header>

  <div class="grid">
    <!-- Capture -->
    <section class="card">
      <h2 class="section-label">capture</h2>
      <textarea
        bind:this={captureRef}
        class="capture-input"
        class:capture-input--sent={sent}
        value={note}
        oninput={(e) => (note = e.target.value)}
        onkeydown={captureKeyDown}
        placeholder={ingestMode ? "paste url..." : "write something..."}
        spellcheck="false"
        aria-label="Quick note"
      ></textarea>
      <footer class="capture-footer">
        <span class="capture-hints">
          <span class="capture-hint" class:capture-hint--error={error}>
            {#if error}
              failed
            {:else if sent}
              sent
            {:else if ingestMode && note.trim()}
              {detectSourceType(note.trim())} · ⌘ enter
            {:else if note.trim()}
              ⌘ enter
            {:else}
              &nbsp;
            {/if}
          </span>
          <span class="capture-hint">{ingestMode ? "⌘I" : "⌘K"}</span>
        </span>
        {#if ingestMode}
          <span class="capture-mode">ingest</span>
        {:else if note.length > 0}
          <span class="capture-count">{note.length}</span>
        {/if}
      </footer>
    </section>

    <!-- Today: events + tasks -->
    <section class="card">
      <h2 class="section-label">today</h2>
      {#if events == null}
        <p class="unavail">calendar unavailable</p>
      {:else if events.length === 0}
        <p class="unavail">no events today</p>
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

      <h2 class="section-label">tasks</h2>
      {#if weeklyOnly.length === 0 && tasksDaily.length === 0}
        <p class="unavail">nothing due</p>
      {:else}
        <ul class="task-list">
          {#each weeklyOnly as task (task.note_id)}
            <li class="task-row">
              <button
                class="task-check"
                aria-pressed={isDone(task)}
                aria-label={`Toggle ${task.title}`}
                onclick={() => toggleTask(task)}
              >
                {isDone(task) ? "☑" : "☐"}
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
                aria-pressed={isDone(task)}
                aria-label={`Toggle ${task.title}`}
                onclick={() => toggleTask(task)}
              >
                {isDone(task) ? "☑" : "☐"}
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

    <!-- Cluster health + alerts -->
    <section class="card">
      <h2 class="section-label">cluster</h2>
      {#if health == null && alerts == null}
        <p class="unavail">unavailable</p>
      {:else}
        {#if allClear}
          <p class="headline headline--ok">
            &#10003; all clear · {health?.scanned ?? 0} workloads scanned
          </p>
        {:else}
          <p class="headline headline--bad">
            {#if health && !health.healthy}
              {unhealthyCount} unhealthy ({unhealthyKinds
                .map(([kind, rows]) => `${rows?.length ?? 0} ${kind}`)
                .join(", ")})
            {:else if health}
              workloads healthy
            {/if}
            {#if alerts && alerts.length > 0}
              · {alerts.length} firing
            {/if}
          </p>
        {/if}
        {#if health == null}
          <p class="unavail">health unavailable</p>
        {:else if !health.healthy}
          <ul class="plain-list">
            {#each unhealthyKinds as [kind, rows]}
              {#each rows ?? [] as row}
                <li class="bad-row">
                  <span class="dim">{kind}</span>
                  {row.namespace ? `${row.namespace}/` : ""}{row.name}
                </li>
              {/each}
            {/each}
          </ul>
        {/if}
        {#if alerts == null}
          <p class="unavail">alerts unavailable</p>
        {:else if alerts.length > 0}
          <ul class="plain-list">
            {#each alerts as alert}
              <li class="alert-row">
                {alert.name}{alert.severity ? ` (${alert.severity})` : ""}
              </li>
            {/each}
          </ul>
        {/if}
      {/if}
    </section>

    <!-- Shipping: PRs + merges -->
    <section class="card">
      <h2 class="section-label">shipping</h2>
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
                  <span class="pr-num">#{pr.number}</span>
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
          <h3 class="sub-label">merged</h3>
          <ul class="plain-list">
            {#each github.recent_merges ?? [] as pr}
              <li>
                <a
                  class="pr-row pr-row--merged"
                  href={pr.url}
                  target="_blank"
                  rel="noopener"
                >
                  <span class="pr-num">#{pr.number}</span>
                  <span class="pr-title">{pr.title}</span>
                  <span class="dim">{relTime(pr.merged_at)}</span>
                </a>
              </li>
            {/each}
          </ul>
        {/if}
      {/if}
    </section>

    <!-- Queues -->
    <section class="card">
      <h2 class="section-label">queues</h2>
      {#if queues == null}
        <p class="unavail">unavailable</p>
      {:else}
        <p class="queue-counts">
          <a href="/review" class="queue-link"
            >{queues.notes_review_queue ?? 0} notes</a
          >
          · {queues.gaps_review_queue ?? 0} gaps in review
        </p>
        {#if (queues.scheduler_jobs ?? []).length > 0}
          <ul class="plain-list">
            {#each queues.scheduler_jobs ?? [] as job}
              <li class="job-row" class:job-row--bad={job.last_status !== "ok"}>
                <span class="job-name">{job.name}</span>
                <span class="job-status">{job.last_status ?? "never ran"}</span>
                <span class="dim">{relTime(job.last_run_at)}</span>
              </li>
            {/each}
          </ul>
        {/if}
      {/if}
    </section>

    <!-- Ask the cluster -->
    <section class="card">
      <h2 class="section-label">ask the cluster</h2>
      <ClusterChatPanel />
    </section>

    <!-- Launcher -->
    <section class="card card--wide">
      <h2 class="section-label">launcher</h2>
      <div class="launcher-grid">
        {#each launcher as item}
          <a
            href={item.href}
            class="launch"
            target={item.external ? "_blank" : undefined}
            rel={item.external ? "noopener" : undefined}
          >
            <span class="launch-label">{item.label}</span>
            <span class="launch-desc">{item.desc}</span>
          </a>
        {/each}
      </div>
    </section>
  </div>
</div>

{#if searchOpen}
  <div class="search-overlay">
    <div class="search-container">
      <input
        type="text"
        class="search-input"
        placeholder="search knowledge..."
        bind:value={searchQuery}
        bind:this={searchInputRef}
      />
      <div class="search-type-filters">
        {#each ["all", "note", "paper", "article", "recipe"] as type}
          <button
            class="search-type-pill"
            class:active={searchType === type}
            onclick={() => (searchType = type)}
          >
            {type}
          </button>
        {/each}
      </div>
      {#if selectedNote}
        <div class="search-preview">
          <div class="search-preview-header">
            <button
              class="search-back"
              onclick={() => {
                selectedNote = null;
              }}
            >
              &larr; back &middot; esc
            </button>
            <h2 class="search-preview-title">{selectedNote.title}</h2>
            {#if selectedNote.tags?.length}
              <div class="search-preview-tags">
                {selectedNote.tags.join(" · ")}
              </div>
            {/if}
          </div>
          <div class="search-preview-content">
            {#each renderNote(selectedNote.content) as block}
              {#if block.tag === "h1"}
                <h1 id={block.id}>{block.text}</h1>
              {:else if block.tag === "h2"}
                <h2 id={block.id}>{block.text}</h2>
              {:else if block.tag === "h3"}
                <h3 id={block.id}>{block.text}</h3>
              {:else}
                <p>{block.text}</p>
              {/if}
            {/each}
          </div>
        </div>
      {:else}
        {#if searchError}
          <p class="search-status search-status--error">{searchError}</p>
        {:else if searching && searchResults.length === 0}
          <p class="search-status">searching...</p>
        {:else if !searching && searchQuery.length >= 2 && searchResults.length === 0}
          <p class="search-status">no results</p>
        {/if}
        {#if searchResults.length > 0}
          <ul
            class="search-results"
            class:search-results--stale={searching}
            role="listbox"
            aria-label="Search results"
          >
            {#each searchResults as result, i}
              <li
                class="search-result"
                class:active={activeIndex === i}
                role="option"
                aria-selected={activeIndex === i}
                onclick={() => {
                  activeIndex = i;
                  selectResult(result);
                }}
                onkeydown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    activeIndex = i;
                    selectResult(result);
                  }
                }}
              >
                <div class="search-result-title">
                  {result.title}
                  {#if result.type}
                    <span class="search-result-badge">{result.type}</span>
                  {/if}
                </div>
                {#if result.section || result.tags?.length}
                  <div class="search-result-meta">
                    {#if result.section}{result.section}{/if}
                    {#if result.section && result.tags?.length}
                      &nbsp;&middot;&nbsp;
                    {/if}
                    {#if result.tags?.length}
                      {result.tags.join(" · ")}
                    {/if}
                  </div>
                {/if}
                {#if result.snippet}
                  <div class="search-result-snippet">{result.snippet}</div>
                {/if}
              </li>
            {/each}
          </ul>
        {/if}
      {/if}
    </div>
  </div>
{/if}

<style>
  /* ── Layout ────────────────────────────────── */

  .dash {
    min-height: 100vh;
    width: 100%;
    max-width: 90rem;
    margin: 0 auto;
    padding: 2rem 1.25rem 3rem 1.25rem;
    font-family: var(--font);
    font-size: 1rem;
    line-height: 1.5;
    color: var(--fg);
    background: var(--bg);
    -webkit-font-feature-settings: "liga" 0;
    font-feature-settings: "liga" 0;
  }

  .dash-header {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    margin-bottom: 1.25rem;
  }

  .date {
    font-size: 0.8rem;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .clock {
    font-size: 0.8rem;
    font-weight: 700;
    color: var(--fg);
    font-variant-numeric: tabular-nums;
  }

  .grid {
    display: grid;
    grid-template-columns: 1fr;
    gap: 1rem;
  }

  @media (min-width: 900px) {
    .grid {
      grid-template-columns: repeat(2, 1fr);
    }
  }

  @media (min-width: 1400px) {
    .grid {
      grid-template-columns: repeat(3, 1fr);
    }
  }

  .card {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    border: 0.06rem solid var(--border);
    padding: 1.1rem 1.25rem 1.25rem 1.25rem;
    min-width: 0;
  }

  .card--wide {
    grid-column: 1 / -1;
  }

  .section-label {
    font-size: 0.65rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.14em;
    color: var(--fg);
    margin: 0 0 0.25rem 0;
    padding-bottom: 0.4rem;
    border-bottom: 0.04rem solid var(--border);
  }

  .sub-label {
    font-size: 0.6rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.14em;
    color: var(--fg-tertiary);
    margin: 0.5rem 0 0 0;
  }

  .unavail {
    font-size: 0.8rem;
    color: var(--fg-tertiary);
    margin: 0;
  }

  .dim {
    color: var(--fg-tertiary);
    font-size: 0.75rem;
  }

  .plain-list {
    list-style: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
  }

  /* ── Capture ───────────────────────────────── */

  .capture-input {
    flex: 1;
    min-height: 9rem;
    resize: none;
    border: none;
    outline: none;
    background: transparent;
    font-family: var(--font);
    font-size: 1.05rem;
    line-height: 1.8;
    color: var(--fg);
    padding: 0;
    letter-spacing: -0.01em;
    transition: opacity 0.3s ease;
  }

  .capture-input::placeholder {
    color: var(--fg-tertiary);
  }

  .capture-input--sent {
    opacity: 0.1;
  }

  .capture-footer {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding-top: 0.5rem;
  }

  .capture-hint {
    font-size: 0.75rem;
    color: var(--fg-tertiary);
    letter-spacing: 0.04em;
    transition: opacity 0.2s ease;
  }

  .capture-hints {
    display: flex;
    gap: 1rem;
    align-items: center;
  }

  .capture-hint--error {
    color: var(--danger);
  }

  .capture-count {
    font-size: 0.75rem;
    color: var(--fg-tertiary);
    opacity: 0.6;
    font-variant-numeric: tabular-nums;
  }

  .capture-mode {
    font-size: 0.65rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.14em;
    color: var(--fg-tertiary);
  }

  /* ── Schedule ──────────────────────────────── */

  .event-list {
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
    padding: 0;
    margin: 0;
  }

  .event-row {
    display: flex;
    align-items: baseline;
    gap: 0.8rem;
    padding: 0.3rem 0;
  }

  .event-time {
    font-size: 0.8rem;
    color: var(--fg-secondary);
    font-variant-numeric: tabular-nums;
    min-width: 3.2rem;
    flex-shrink: 0;
  }

  .event-title {
    font-size: 0.95rem;
    flex: 1;
    min-width: 0;
  }

  .event-location {
    display: block;
    font-size: 0.75rem;
    color: var(--fg-tertiary);
  }

  .event-meta {
    font-size: 0.6rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--fg-tertiary);
    flex-shrink: 0;
  }

  .event-row--active {
    font-weight: 700;
  }

  .event-row--active .event-time {
    color: var(--fg);
  }

  .event-row--past .event-time,
  .event-row--past .event-title,
  .event-row--past .event-meta {
    text-decoration: line-through;
    opacity: 0.3;
  }

  /* ── Tasks ─────────────────────────────────── */

  .task-list {
    list-style: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
  }

  .task-row {
    display: flex;
    align-items: baseline;
    gap: 0.5rem;
    padding: 0.15rem 0;
  }

  .task-check {
    all: unset;
    font-family: var(--font);
    font-size: 0.9rem;
    color: var(--fg-secondary);
    cursor: pointer;
    flex-shrink: 0;
  }

  .task-check:hover {
    color: var(--fg);
  }

  .task-check:focus-visible {
    outline: 1.5px solid var(--fg);
    outline-offset: 2px;
  }

  .task-title {
    font-size: 0.85rem;
    flex: 1;
    min-width: 0;
  }

  .task-title--weekly {
    font-weight: 700;
  }

  .task-title--done {
    text-decoration: line-through;
    opacity: 0.3;
  }

  .task-due {
    font-size: 0.7rem;
    color: var(--fg-tertiary);
    font-variant-numeric: tabular-nums;
    flex-shrink: 0;
  }

  /* ── Cluster ───────────────────────────────── */

  .headline {
    font-size: 0.9rem;
    font-weight: 700;
    margin: 0;
  }

  .headline--ok {
    color: var(--fg);
  }

  .headline--bad {
    color: var(--danger);
  }

  .bad-row {
    font-size: 0.85rem;
    color: var(--fg);
  }

  .alert-row {
    font-size: 0.85rem;
    color: var(--danger);
  }

  /* ── Shipping ──────────────────────────────── */

  .pr-row {
    display: flex;
    align-items: baseline;
    gap: 0.5rem;
    padding: 0.15rem 0;
    color: var(--fg-secondary);
    text-decoration: none;
    transition: color 0.15s ease;
    min-width: 0;
  }

  .pr-row:hover {
    color: var(--fg);
  }

  .pr-num {
    font-size: 0.8rem;
    color: var(--fg-tertiary);
    font-variant-numeric: tabular-nums;
    flex-shrink: 0;
  }

  .pr-title {
    font-size: 0.85rem;
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .ci-dot {
    width: 0.5rem;
    height: 0.5rem;
    border-radius: 50%;
    flex-shrink: 0;
    align-self: center;
  }

  .ci-dot--passing {
    background: var(--st-ok);
  }

  .ci-dot--failing {
    background: var(--danger);
  }

  .ci-dot--pending {
    background: var(--fg-tertiary);
    opacity: 0.4;
  }

  /* ── Queues ────────────────────────────────── */

  .queue-counts {
    font-size: 0.85rem;
    color: var(--fg-secondary);
    margin: 0;
  }

  .queue-link {
    color: var(--fg);
    font-weight: 700;
    text-decoration: none;
  }

  .queue-link:hover {
    color: var(--fg-secondary);
  }

  .job-row {
    display: flex;
    align-items: baseline;
    gap: 0.5rem;
    font-size: 0.8rem;
    color: var(--fg-secondary);
  }

  .job-name {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .job-status {
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--fg-tertiary);
    flex-shrink: 0;
  }

  .job-row--bad .job-status {
    color: var(--danger);
  }

  /* ── Launcher ──────────────────────────────── */

  .launcher-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(11rem, 1fr));
    gap: 0.5rem;
  }

  .launch {
    display: flex;
    flex-direction: column;
    gap: 0.1rem;
    padding: 0.6rem 0.75rem;
    border: 0.06rem solid var(--border);
    color: var(--fg-secondary);
    text-decoration: none;
    transition:
      color 0.15s ease,
      border-color 0.15s ease;
    min-width: 0;
  }

  .launch:hover {
    color: var(--fg);
    border-color: var(--fg);
  }

  .launch:focus-visible {
    outline: 1.5px solid var(--fg);
    outline-offset: 2px;
  }

  .launch-label {
    font-size: 0.85rem;
    font-weight: 700;
    color: var(--fg);
  }

  .launch-desc {
    font-size: 0.7rem;
    color: var(--fg-tertiary);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  /* ── Knowledge search overlay ───────────────── */

  .search-overlay {
    position: fixed;
    inset: 0;
    background: var(--bg);
    z-index: 100;
    overflow-y: auto;
  }

  .search-container {
    max-width: 72ch;
    margin: 0 auto;
    padding: 2.5rem;
  }

  .search-input {
    width: 100%;
    font-family: var(--font);
    font-size: 1.1rem;
    background: transparent;
    border: none;
    border-bottom: 0.06rem solid var(--border);
    padding: 0.5rem 0;
    color: var(--fg);
    outline: none;
  }

  .search-input::placeholder {
    color: var(--fg-tertiary);
  }

  .search-type-filters {
    display: flex;
    gap: 1rem;
    margin: 1rem 0;
  }

  .search-type-pill {
    font-size: 0.65rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.14em;
    color: var(--fg-tertiary);
    cursor: pointer;
    background: none;
    border: none;
    padding: 0;
    font-family: var(--font);
  }

  .search-type-pill.active {
    color: var(--fg);
  }

  .search-results {
    list-style: none;
    padding: 0;
    margin: 1rem 0 0 0;
  }

  .search-results--stale {
    opacity: 0.5;
  }

  .search-result {
    padding: 0.75rem 0;
    border-bottom: 0.04rem solid var(--border);
    cursor: pointer;
  }

  .search-result.active {
    background: var(--surface);
  }

  .search-result-title {
    font-weight: 700;
    color: var(--fg);
  }

  .search-result-badge {
    font-size: 0.65rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.14em;
    color: var(--fg-tertiary);
    margin-left: 0.5rem;
  }

  .search-result-meta {
    font-size: 0.75rem;
    color: var(--fg-tertiary);
    margin-top: 0.25rem;
  }

  .search-result-snippet {
    font-size: 0.85rem;
    color: var(--fg-secondary);
    margin-top: 0.25rem;
    line-height: 1.5;
  }

  .search-status {
    color: var(--fg-tertiary);
    margin-top: 1rem;
    font-size: 0.85rem;
  }

  .search-status--error {
    color: var(--danger);
  }

  /* ── Note preview ─────────────────────────── */

  .search-preview-header {
    border-bottom: 0.06rem solid var(--border);
    padding-bottom: 1rem;
    margin-bottom: 1.5rem;
  }
  .search-back {
    font-family: var(--font);
    font-size: 0.75rem;
    color: var(--fg-tertiary);
    background: none;
    border: none;
    padding: 0;
    cursor: pointer;
    margin-bottom: 1rem;
  }
  .search-back:hover {
    color: var(--fg-secondary);
  }
  .search-preview-title {
    font-size: 1.25rem;
    font-weight: 700;
    color: var(--fg);
    margin: 0;
  }
  .search-preview-tags {
    font-size: 0.75rem;
    color: var(--fg-tertiary);
    margin-top: 0.5rem;
  }
  .search-preview-content h1,
  .search-preview-content h2,
  .search-preview-content h3 {
    color: var(--fg);
    margin: 1.5rem 0 0.75rem 0;
  }
  .search-preview-content h1 {
    font-size: 1.15rem;
  }
  .search-preview-content h2 {
    font-size: 1rem;
  }
  .search-preview-content h3 {
    font-size: 0.9rem;
  }
  .search-preview-content p {
    color: var(--fg-secondary);
    line-height: 1.6;
    margin: 0 0 1rem 0;
    white-space: pre-wrap;
  }
</style>
