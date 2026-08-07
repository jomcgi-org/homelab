<script>
  import { tick } from "svelte";
  import { renderAgentMarkdown } from "./markdown.js";
  import { statusClass, statusLabel, vmState } from "./status.js";
  import "./agents-theme.css";

  let { data } = $props();

  const MODELS = ["opus", "fable", "sonnet", "luna", "terra", "sol", "qwen"];
  const RECENT_HISTORY_MS = 24 * 60 * 60 * 1000;
  const ATTENTION_MS = 60 * 60 * 1000;

  let selectedId = $state(null);
  let sidebarCollapsed = $state(false);
  if (typeof window !== "undefined") {
    try {
      sidebarCollapsed =
        window.localStorage.getItem("agents-sidebar-collapsed") === "true";
    } catch (e) {
      // localStorage blocked; continue with expanded
    }
  }
  let sessions = $state(data.sessions ?? []);
  let detail = $state(null);
  let searchQuery = $state("");
  let searchResults = $state(null);
  let searchLoading = $state(false);
  let prompt = $state("");
  let composerModel = $state("");
  let sending = $state(false);
  let creating = $state(false);
  let showNewPanel = $state(false);
  let showAllHistory = $state(false);
  let repos = $state([]);
  let branches = $state([]);
  let repoLoading = $state(false);
  let branchLoading = $state(false);
  let reposLoaded = $state(false);
  let branchLoadSequence = 0;
  let newSession = $state({
    prompt: "",
    model: "",
    repo: "",
    branch: "",
  });
  let errorMessage = $state(
    data.error ? "Unable to load agent sessions" : null,
  );
  let searchTimer = null;
  let searchController = null;
  let requestSequence = 0;
  let renderedPending = $state({});
  let vms = $state({});
  let turnsEl = $state(null);

  const selectedSession = $derived(
    sessions.find((session) => String(session.id) === String(selectedId)) ??
      detail?.session ??
      null,
  );
  const activeSessions = $derived(
    sessions.filter((session) => isActive(session)).sort(compareSessions),
  );
  const historySessions = $derived(
    sessions.filter((session) => !isActive(session)).sort(compareSessions),
  );
  const recentHistorySessions = $derived(
    historySessions.filter(
      (session) => Date.now() - lastActiveAt(session) < RECENT_HISTORY_MS,
    ),
  );
  const visibleHistorySessions = $derived(
    showAllHistory ? historySessions : recentHistorySessions,
  );
  const visibleSearchResults = $derived(searchResults ?? []);
  const hasActiveSessions = $derived(
    sessions.some((session) => isActive(session)) ||
      Number(
        sessions.find((session) => String(session.id) === String(selectedId))
          ?.pending_count,
      ) > 0,
  );
  const headerTitle = $derived(
    firstLine(selectedSession?.title) ||
      firstLine(detail?.turns?.[0]?.prompt) ||
      firstLine(detail?.pending_queue?.[0]?.prompt) ||
      sessionTitle(selectedSession),
  );

  function isActive(session) {
    return session?.status === "running" || Number(session?.pending_count) > 0;
  }

  function compareSessions(a, b) {
    const aTime = Date.parse(timestamp(a?.last_turn_at ?? a?.created_at));
    const bTime = Date.parse(timestamp(b?.last_turn_at ?? b?.created_at));
    return (
      (Number.isNaN(bTime) ? 0 : bTime) - (Number.isNaN(aTime) ? 0 : aTime)
    );
  }

  function lastActiveAt(session) {
    const at = Date.parse(
      timestamp(session?.last_turn_at ?? session?.created_at),
    );
    return Number.isNaN(at) ? 0 : at;
  }

  function timestamp(value) {
    if (!value) return "";
    return /(?:Z|[+-]\d\d:\d\d)$/.test(value) ? value : `${value}Z`;
  }

  function relativeTime(value) {
    if (!value) return "never";
    const then = Date.parse(timestamp(value));
    if (Number.isNaN(then)) return "unknown";
    const seconds = Math.max(0, Math.floor((Date.now() - then) / 1000));
    if (seconds < 60) return "now";
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes}m`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours}h`;
    const days = Math.floor(hours / 24);
    if (days === 1) return "yesterday";
    if (days < 30) return `${days}d`;
    const months = Math.floor(days / 30);
    if (months < 12) return `${months}mo`;
    return `${Math.floor(months / 12)}y`;
  }

  function firstLine(value) {
    return String(value || "")
      .trim()
      .split("\n")[0];
  }

  function shortId(session) {
    return String(session?.local_session_id || session?.id || "session").slice(
      0,
      8,
    );
  }

  function sessionTitle(session) {
    return (
      firstLine(session?.title) ||
      (session?.repo ? `${session.repo}@${session.branch || "main"}` : "") ||
      shortId(session)
    );
  }

  function formatRepoContext(session) {
    if (session?.repo) return `${session.repo} @ ${session.branch || "main"}`;
    if (session?.workspace && session.workspace !== "<guest>") {
      return session.workspace;
    }
    return "scratch workspace";
  }

  function sidebarDot(session) {
    const cls = statusClass(session);
    if (cls === "running" || cls === "working" || cls === "needs_input") {
      return cls;
    }
    if (cls === "warn" && Date.now() - lastActiveAt(session) < ATTENTION_MS) {
      return "warn";
    }
    return "idle";
  }

  function cost(value) {
    const amount = Number(value || 0);
    if (!(amount > 0)) return "";
    return amount >= 0.01 ? `$${amount.toFixed(2)}` : `$${amount.toFixed(4)}`;
  }

  // Mirrors the backend's _CLEAN_TERMINAL_REASONS: "completed"/"end_turn"
  // from the claude lane, "stop" from the pi lane's raw stopReason. A
  // qwen turn is a normal success with terminal_reason "stop", and the
  // old !== "completed" check painted every one of them as failed.
  const CLEAN_TERMINAL_REASONS = new Set(["completed", "end_turn", "stop"]);

  function turnFailed(turn) {
    return Boolean(
      turn?.terminal_reason &&
      !CLEAN_TERMINAL_REASONS.has(turn.terminal_reason),
    );
  }

  function activityParts(activity) {
    if (typeof activity === "string") return { verb: activity, detail: "" };
    if (!activity || typeof activity !== "object") {
      return { verb: "step", detail: "" };
    }
    const kind = String(
      activity.type || activity.tool || activity.name || "",
    ).toLowerCase();
    if (kind === "edit" || kind === "write") {
      return { verb: kind, detail: activity.file_path || activity.path || "" };
    }
    if (kind === "bash" || kind === "shell") {
      return {
        verb: "run",
        detail: activity.command || compactInput(activity.input),
      };
    }
    if (activity.name) {
      return {
        verb: String(activity.name),
        detail: compactInput(activity.input),
      };
    }
    return { verb: kind || "step", detail: compactInput(activity.input) };
  }

  function compactInput(input) {
    if (input == null) return "";
    const text = typeof input === "string" ? input : JSON.stringify(input);
    return text.length > 110 ? `${text.slice(0, 110)}…` : text;
  }

  function activityLine(activity) {
    const { verb, detail } = activityParts(activity);
    return detail ? `${verb} ${detail}` : verb;
  }

  function liveStateLabel(entry, index) {
    if (entry.claimed_by_replica) return "working";
    return index === 0 ? "starting" : "waiting";
  }

  function autoScroll(force = false) {
    if (!turnsEl) return;
    const nearBottom =
      turnsEl.scrollHeight - turnsEl.scrollTop - turnsEl.clientHeight < 200;
    if (force || nearBottom) turnsEl.scrollTop = turnsEl.scrollHeight;
  }

  function syncPendingPartials(entries) {
    const next = {};
    for (const entry of entries ?? []) {
      const current = renderedPending[entry.seq];
      const newPartialText = entry.partial_text;
      const newPartialActivities = entry.partial_activities;
      if (
        current?.partial_text !== newPartialText ||
        JSON.stringify(current?.partial_activities) !==
          JSON.stringify(newPartialActivities)
      ) {
        next[entry.seq] = {
          partial_text: newPartialText,
          partial_activities: newPartialActivities,
        };
      } else {
        next[entry.seq] = current;
      }
    }
    renderedPending = next;
  }

  async function loadDetail(
    id,
    sequence = requestSequence,
    incremental = false,
  ) {
    if (id == null) return;
    try {
      const maxSeq = incremental
        ? Math.max(0, ...(detail?.turns ?? []).map((turn) => turn.seq))
        : 0;
      const suffix = incremental ? `?after_seq=${maxSeq}` : "";
      const response = await fetch(
        `/agents/session/${encodeURIComponent(id)}${suffix}`,
      );
      if (!response.ok) throw new Error("Unable to load session");
      const body = await response.json();
      if (sequence === requestSequence && String(selectedId) === String(id)) {
        syncPendingPartials(body.pending_queue);
        detail = incremental
          ? {
              session: body.session,
              turns: [
                ...(detail?.turns ?? []),
                ...(body.turns ?? []).filter(
                  (turn) =>
                    !(detail?.turns ?? []).some(
                      (existing) => existing.seq === turn.seq,
                    ),
                ),
              ],
              pending_queue: body.pending_queue,
            }
          : body;
        const force = !incremental;
        tick().then(() => autoScroll(force));
      }
    } catch (error) {
      if (sequence === requestSequence) errorMessage = error.message;
    }
  }

  async function loadSessions() {
    try {
      const response = await fetch("/agents/data");
      if (!response.ok) throw new Error("Unable to refresh sessions");
      const body = await response.json();
      sessions = Array.isArray(body) ? body : (body.sessions ?? []);
      errorMessage = null;
      if (
        selectedId != null &&
        !sessions.some((session) => String(session.id) === String(selectedId))
      ) {
        selectedId = null;
        detail = null;
      }
    } catch (error) {
      errorMessage = error.message;
    }
  }

  async function loadVms() {
    try {
      const response = await fetch("/agents/vms");
      if (!response.ok) return;
      const body = await response.json();
      vms = body.vms ?? {};
    } catch {
      // VM state is advisory; keep the last known map on transient failures.
    }
  }

  async function loadRepos() {
    repoLoading = true;
    try {
      const response = await fetch("/agents/repos");
      if (!response.ok) throw new Error("Unable to load repos");
      const body = await response.json();
      repos = body.repos ?? [];
    } catch (error) {
      errorMessage = error.message;
    } finally {
      repoLoading = false;
      reposLoaded = true;
    }
  }

  async function loadBranches(repoId) {
    if (!repoId) {
      branches = [];
      return;
    }
    branchLoading = true;
    const generation = ++branchLoadSequence;
    const [owner, repo] = repoId.split("/");
    try {
      const response = await fetch(
        `/agents/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}/branches`,
      );
      if (!response.ok) throw new Error("Unable to load branches");
      const body = await response.json();
      if (generation === branchLoadSequence) {
        branches = body.branches ?? [];
        newSession.branch = body.default_branch ?? "main";
        if (
          branches.length > 0 &&
          !branches.some((b) => b.name === newSession.branch)
        ) {
          newSession.branch = branches[0].name;
        }
      }
    } catch (error) {
      if (generation === branchLoadSequence) {
        branches = [];
        newSession.branch = "main";
      }
    } finally {
      if (generation === branchLoadSequence) {
        branchLoading = false;
      }
    }
  }

  function selectSession(sessionOrId) {
    const id = typeof sessionOrId === "object" ? sessionOrId?.id : sessionOrId;
    if (id == null) return;
    requestSequence += 1;
    selectedId = id;
    detail = null;
    renderedPending = {};
    searchResults = null;
    loadDetail(id, requestSequence);
    const session = sessions.find((item) => String(item.id) === String(id));
    composerModel = session?.model || "";
  }

  function toggleSidebar() {
    sidebarCollapsed = !sidebarCollapsed;
    if (sidebarCollapsed) {
      searchController?.abort();
      searchController = null;
      searchQuery = "";
      searchResults = null;
      searchLoading = false;
    }
    try {
      document.documentElement.setAttribute(
        "data-agents-rail",
        sidebarCollapsed ? "collapsed" : "expanded",
      );
      localStorage.setItem(
        "agents-sidebar-collapsed",
        String(sidebarCollapsed),
      );
    } catch (e) {
      // localStorage blocked; keep the in-memory preference
    }
  }

  async function runSearch() {
    const query = searchQuery.trim();
    if (!query) {
      searchController?.abort();
      searchController = null;
      searchResults = null;
      searchLoading = false;
      return;
    }
    searchController?.abort();
    const controller = new AbortController();
    searchController = controller;
    searchLoading = true;
    try {
      const response = await fetch(
        `/agents/search?q=${encodeURIComponent(query)}&limit=30`,
        {
          signal: controller.signal,
        },
      );
      if (!response.ok) throw new Error("Search unavailable");
      const body = await response.json();
      if (!controller.signal.aborted) searchResults = body.results ?? [];
    } catch {
      // Keep the previous result set for both aborted and failed requests.
    } finally {
      if (searchController === controller) {
        searchController = null;
        searchLoading = false;
      }
    }
  }

  async function sendPrompt() {
    if (!selectedId || !prompt.trim() || sending) return;
    sending = true;
    errorMessage = null;
    try {
      const requestBody = { prompt: prompt.trim() };
      if (composerModel) requestBody.model = composerModel;
      const response = await fetch(
        `/agents/session/${encodeURIComponent(selectedId)}/messages`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(requestBody),
        },
      );
      const body = await response.json();
      if (!response.ok || body.accepted === false)
        throw new Error(body.error || "Message was not accepted");
      prompt = "";
      await Promise.all([
        loadSessions(),
        loadDetail(selectedId, requestSequence, true),
      ]);
    } catch (error) {
      errorMessage = error.message;
    } finally {
      sending = false;
    }
  }

  async function createSession() {
    if (!newSession.prompt.trim() || creating) return;
    creating = true;
    errorMessage = null;
    try {
      const requestBody = {
        prompt: newSession.prompt.trim(),
      };
      if (newSession.model) requestBody.model = newSession.model;
      if (newSession.repo) {
        requestBody.repo = newSession.repo;
        requestBody.branch = newSession.branch;
      }
      const response = await fetch("/agents/sessions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(requestBody),
      });
      const body = await response.json();
      if (!response.ok || body.accepted === false)
        throw new Error(body.error || "Session was not created");
      creating = false;
      showNewPanel = false;
      newSession = { prompt: "", model: "", repo: "", branch: "" };
      branches = [];
      await loadSessions();
      selectSession(body.session_id);
    } catch (error) {
      errorMessage = error.message;
      creating = false;
    }
  }

  async function destroySession() {
    if (!selectedId || !window.confirm("Destroy this agent session?")) return;
    try {
      const response = await fetch(
        `/agents/session/${encodeURIComponent(selectedId)}`,
        { method: "DELETE" },
      );
      if (!response.ok) throw new Error("Session could not be destroyed");
      selectedId = null;
      detail = null;
      await loadSessions();
    } catch (error) {
      errorMessage = error.message;
    }
  }

  $effect(() => {
    if (selectedId == null && sessions.length) selectSession(sessions[0]);
  });

  $effect(() => {
    searchQuery;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(runSearch, 200);
    return () => clearTimeout(searchTimer);
  });

  $effect(() => {
    selectedId;
    sessions;
    detail;
    const hasClaimed = detail?.pending_queue?.some(
      (pending) => pending.claimed_by_replica,
    );
    if (hasClaimed && selectedId != null) {
      let stopped = false;
      let timeoutHandle;
      const schedulePoll = async () => {
        if (stopped) return;
        const currentSequence = ++requestSequence;
        const startTime = Date.now();
        await loadDetail(selectedId, currentSequence, true);
        if (stopped || currentSequence !== requestSequence) return;
        const elapsed = Date.now() - startTime;
        const delayUntilNext = Math.max(0, 100 - elapsed);
        timeoutHandle = setTimeout(schedulePoll, delayUntilNext);
      };
      // On success, the effect re-run (from detail state change) schedules
      // the next poll via the initial 100ms timeout.
      // On error, in-loop self-schedule handles the retry to avoid blocking.
      timeoutHandle = setTimeout(schedulePoll, 100);
      const interval = setInterval(loadSessions, 2000);
      return () => {
        stopped = true;
        clearTimeout(timeoutHandle);
        clearInterval(interval);
      };
    }
    const pollInterval = hasActiveSessions ? 2000 : 15000;
    const interval = setInterval(async () => {
      await loadSessions();
      if (selectedId != null)
        await loadDetail(selectedId, requestSequence, true);
    }, pollInterval);
    return () => clearInterval(interval);
  });

  $effect(() => {
    if (showNewPanel && !reposLoaded && !repoLoading) {
      loadRepos();
    }
  });

  // Guest VM state polls on its own cadence: the control plane parks a VM
  // idleBankSeconds (20s) after its last invoke without telling the
  // monolith, so a fixed 5s poll keeps the chip honest even when the
  // session list has dropped to its slow 15s interval.
  $effect(() => {
    loadVms();
    const interval = setInterval(loadVms, 5000);
    return () => clearInterval(interval);
  });

  $effect(() => () => {
    clearTimeout(searchTimer);
    searchController?.abort();
  });
</script>

<svelte:head><title>Agents</title></svelte:head>

<main class:sidebar-collapsed={sidebarCollapsed} class="console">
  <aside class="sidebar" aria-label="Agent sessions">
    <div class="side-head">
      <div class="side-head-left">
        <div class="eyebrow">agent sessions</div>
        <button
          class="collapse-button"
          type="button"
          aria-label={sidebarCollapsed
            ? "Expand session sidebar"
            : "Collapse session sidebar"}
          aria-expanded={!sidebarCollapsed}
          title={sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar"}
          onclick={toggleSidebar}>{sidebarCollapsed ? "→" : "←"}</button
        >
      </div>
      <button
        class="new-button"
        type="button"
        onclick={() => (showNewPanel = !showNewPanel)}>+ new</button
      >
    </div>

    <label class="search-label">
      <span class="sr-only">Search sessions</span>
      <input
        bind:value={searchQuery}
        placeholder="search transcripts"
        autocomplete="off"
      />
      {#if searchLoading}<span class="search-pulse">…</span>{/if}
    </label>

    {#if searchResults !== null}
      <div class="group-title">Search results</div>
      <div class="session-list">
        {#each visibleSearchResults as result (result.session_id + ":" + result.seq)}
          <button
            class="search-result"
            type="button"
            onclick={() => selectSession(result.session_id)}
          >
            <span class="snippet">{result.snippet}</span>
            <span class="result-meta mono"
              >{String(result.local_session_id || result.workspace || "").slice(
                0,
                8,
              )} · turn {result.seq} · {relativeTime(result.created_at)}</span
            >
          </button>
        {:else}<div class="empty">No matching turns</div>{/each}
      </div>
    {:else}
      {#if activeSessions.length}
        <div class="group-title">
          Active <span>{activeSessions.length}</span>
        </div>
        <div class="session-list">
          {#each activeSessions as session (session.id)}
            {@render sessionRow(session)}
          {/each}
        </div>
      {/if}
      <div class="group-title history-title">Recent</div>
      <div class="session-list">
        {#each visibleHistorySessions as session (session.id)}
          {@render sessionRow(session)}
        {:else}<div class="empty">
            {activeSessions.length ? "No recent sessions" : "No sessions yet"}
          </div>{/each}
      </div>
      {#if historySessions.length > recentHistorySessions.length}
        <button
          class="history-toggle"
          type="button"
          onclick={() => (showAllHistory = !showAllHistory)}
          >{showAllHistory
            ? "show recent only"
            : `show all (${historySessions.length})`}</button
        >
      {/if}
    {/if}
  </aside>

  <section class="transcript" aria-label="Agent transcript">
    {#if selectedSession}
      <header class="transcript-head">
        <div class="head-main">
          <h1 class="session-title" title={headerTitle}>{headerTitle}</h1>
          <div class="session-context mono">
            {formatRepoContext(selectedSession)} · {selectedSession.model ||
              "luna"} · {shortId(selectedSession)}
          </div>
        </div>
        <div class="head-actions">
          <span
            class={`vm-chip vm-${vmState(selectedSession, vms)}`}
            title={vms[selectedSession.ember_session_id]?.cp_state
              ? `control plane: ${vms[selectedSession.ember_session_id].cp_state}`
              : "no live microVM; the next prompt boots fresh"}
            >vm {vmState(selectedSession, vms)}</span
          >
          {#if statusClass(selectedSession) !== "completed"}
            <span class={`session-state ${statusClass(selectedSession)}`}
              >{statusLabel(selectedSession)}</span
            >
          {/if}
          <button class="destroy-button" type="button" onclick={destroySession}
            >destroy</button
          >
        </div>
      </header>
      <div class="turns" bind:this={turnsEl}>
        <div class="turns-inner">
          {#each detail?.turns ?? [] as turn (turn.seq)}
            <article class="turn">
              <div class="prompt">
                <span class="role">you</span>
                <div class="prompt-text">{turn.prompt}</div>
              </div>
              {#if turn.usage?.activities?.length}
                <details class="steps">
                  <summary
                    >{turn.usage.activities.length}
                    {turn.usage.activities.length === 1
                      ? "step"
                      : "steps"}</summary
                  >
                  <ol class="step-list">
                    {#each turn.usage.activities as activity}
                      <li>
                        <span class="step-verb"
                          >{activityParts(activity).verb}</span
                        >
                        <span class="step-detail"
                          >{activityParts(activity).detail}</span
                        >
                      </li>
                    {/each}
                  </ol>
                </details>
              {/if}
              {#if turnFailed(turn)}
                <pre class="turn-error">{turn.result_text ||
                    "The turn failed without output."}</pre>
              {:else if turn.result_text}
                <div class="result-md">
                  {@html renderAgentMarkdown(turn.result_text)}
                </div>
              {/if}
              <div class="turn-meta mono">
                <span>{turn.model || selectedSession.model || "luna"}</span>
                <span>{relativeTime(turn.created_at)}</span>
                {#if cost(turn.cost_usd)}<span>{cost(turn.cost_usd)}</span>{/if}
                {#if turn.stop_reason && turn.stop_reason !== "end_turn"}
                  <span>{turn.stop_reason}</span>
                {/if}
                {#if turnFailed(turn)}<span class="badge-failed">failed</span
                  >{/if}
              </div>
            </article>
          {/each}

          {#each detail?.pending_queue ?? [] as entry, index (entry.seq)}
            {@const partial = renderedPending[entry.seq]}
            {@const state = liveStateLabel(entry, index)}
            <article class="turn live">
              <div class="prompt">
                <span class="role">you</span>
                <div class="prompt-text">{entry.prompt}</div>
              </div>
              <div class={`live-line ${state === "working" ? "" : "quiet"}`}>
                <span class="live-dot" aria-hidden="true"></span>
                {#if state === "working"}
                  {#if partial?.partial_activities?.length}
                    <span class="live-latest"
                      >{activityLine(
                        partial.partial_activities[
                          partial.partial_activities.length - 1
                        ],
                      )}</span
                    >
                  {:else}
                    <span class="live-latest">working…</span>
                  {/if}
                {:else if state === "starting"}
                  <span class="live-latest">starting up…</span>
                {:else}
                  <span class="live-latest">waiting for the turn ahead…</span>
                {/if}
              </div>
              {#if partial?.partial_activities?.length > 1}
                <ol class="step-list live-steps" aria-label="Agent activity">
                  {#if partial.partial_activities.length > 6}
                    <li class="step-earlier">
                      … {partial.partial_activities.length - 6} earlier steps
                    </li>
                  {/if}
                  {#each partial.partial_activities.slice(-6) as activity}
                    <li>
                      <span class="step-verb"
                        >{activityParts(activity).verb}</span
                      >
                      <span class="step-detail"
                        >{activityParts(activity).detail}</span
                      >
                    </li>
                  {/each}
                </ol>
              {/if}
              {#if partial?.partial_text}
                <div class="result-md">
                  {@html renderAgentMarkdown(partial.partial_text)}
                </div>
              {/if}
            </article>
          {/each}

          {#if !(detail?.turns ?? []).length && !(detail?.pending_queue ?? []).length}
            <div class="empty transcript-empty">
              {detail
                ? "No turns yet. Send a prompt below."
                : "Loading session…"}
            </div>
          {/if}
        </div>
      </div>

      <form
        class="composer"
        onsubmit={(event) => {
          event.preventDefault();
          sendPrompt();
        }}
      >
        <div class="composer-inner">
          <textarea
            bind:value={prompt}
            placeholder="send a prompt to this session (⌘⏎ to send)"
            rows="3"
            onkeydown={(e) => {
              if (
                (e.metaKey || e.ctrlKey) &&
                e.key === "Enter" &&
                !e.isComposing &&
                !sending &&
                prompt.trim()
              ) {
                e.preventDefault();
                sendPrompt();
              }
            }}></textarea>
          <div class="composer-actions">
            {@render modelPicker(composerModel, (model) => {
              composerModel = model;
            })}
            <button
              class="send-button"
              type="submit"
              disabled={sending || !prompt.trim()}
              >{sending ? "sending…" : "send"}</button
            >
          </div>
        </div>
      </form>
    {:else}
      <div class="empty blank-state">Select a session or create a new one</div>
    {/if}
  </section>

  {#if showNewPanel}
    <section class="new-panel">
      <div class="eyebrow">new session</div>
      <form
        onsubmit={(event) => {
          event.preventDefault();
          createSession();
        }}
      >
        <label
          >prompt<textarea
            bind:value={newSession.prompt}
            rows="7"
            placeholder="what should the agent do?"
            onkeydown={(e) => {
              if (
                (e.metaKey || e.ctrlKey) &&
                e.key === "Enter" &&
                !e.isComposing &&
                !creating &&
                newSession.prompt.trim()
              ) {
                e.preventDefault();
                createSession();
              }
            }}></textarea></label
        >
        <div class="field">
          <span class="field-label">model</span>
          {@render modelPicker(newSession.model, (model) => {
            newSession.model = model;
          })}
        </div>
        <label
          >repo<select
            class="mono"
            bind:value={newSession.repo}
            disabled={repoLoading}
            onchange={() => {
              newSession.branch = "";
              loadBranches(newSession.repo);
            }}
          >
            {#if repoLoading}
              <option value="">loading repos</option>
            {:else}
              <option value="">none (scratch workspace)</option>
              {#each repos as repo}
                <option value={repo.id} title={repo.description || ""}>
                  {repo.id}
                </option>
              {/each}
            {/if}
          </select></label
        >
        <label>
          branch<select
            class="mono"
            bind:value={newSession.branch}
            disabled={!newSession.repo || branchLoading}
          >
            {#if branchLoading}
              <option value="">loading branches</option>
            {:else if branches.length === 0}
              <option value="main">main</option>
            {:else}
              {#each branches as branch}
                <option value={branch.name}>{branch.name}</option>
              {/each}
            {/if}
          </select>
        </label>
        <div class="new-actions">
          <button
            type="button"
            class="quiet-button"
            onclick={() => (showNewPanel = false)}>cancel</button
          ><button
            class="send-button"
            type="submit"
            disabled={creating || !newSession.prompt.trim()}
            >{creating ? "creating…" : "create"}</button
          >
        </div>
      </form>
    </section>
  {/if}

  {#if errorMessage}<div class="error-banner" role="status">
      {errorMessage}
    </div>{/if}
</main>

{#snippet sessionRow(session)}
  <button
    class:chosen={String(selectedId) === String(session.id)}
    class="session-row"
    type="button"
    aria-label={`${sessionTitle(session)}: ${statusLabel(session)}`}
    onclick={() => selectSession(session)}
  >
    <span class={`dot ${sidebarDot(session)}`} title={statusLabel(session)}
    ></span>
    <span class="row-main"
      ><span class="session-name">{sessionTitle(session)}</span><span
        class="row-sub mono"
      >
        {session.model || "luna"}
        {#if session.repo}· {(session.repo.split("/")[1] || session.repo) +
            "@" +
            (session.branch || "main")}{/if}
        · {relativeTime(session.last_turn_at || session.created_at)}
      </span></span
    >
    {#if cost(session.total_cost_usd)}
      <span class="row-cost mono">{cost(session.total_cost_usd)}</span>
    {/if}
  </button>
{/snippet}

{#snippet modelPicker(current, choose)}
  <div class="model-chips" role="group" aria-label="Model">
    <button
      type="button"
      class="chip"
      class:on={!current}
      aria-pressed={!current}
      onclick={() => choose("")}>default</button
    >
    {#each MODELS as model}
      <button
        type="button"
        class="chip"
        class:on={current === model}
        aria-pressed={current === model}
        onclick={() => choose(model)}>{model}</button
      >
    {/each}
  </div>
{/snippet}

<style>
  :global(*) {
    box-sizing: border-box;
  }
  :global(body) {
    margin: 0;
  }

  .console {
    --font-ui:
      system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    --font-mono: ui-monospace, SFMono-Regular, Menlo, monospace;
    --size-meta: 11.5px;
    --size-body-mono: 12.5px;
    --size-body: 14px;
    color-scheme: light;
    height: 100vh;
    display: grid;
    grid-template-columns: 300px minmax(0, 1fr);
    background: var(--page-bg);
    color: var(--text);
    font-family: var(--font-ui);
    font-size: var(--size-body);
    line-height: 1.45;
  }
  .console * {
    font-family: var(--font-ui);
  }
  .console .mono,
  .console pre,
  .console input.mono,
  .console select.mono {
    font-family: var(--font-mono);
  }
  .mono,
  pre {
    font-size: var(--size-body-mono);
  }
  button,
  input,
  textarea,
  select {
    font: inherit;
  }
  button {
    cursor: pointer;
  }
  button,
  input,
  textarea,
  select {
    border-radius: 6px;
  }
  button:focus-visible,
  input:focus-visible,
  textarea:focus-visible,
  select:focus-visible,
  .steps summary:focus-visible {
    outline: 2px solid var(--info);
    outline-offset: 1px;
  }
  .sidebar {
    min-height: 0;
    border-right: 1px solid var(--line);
    padding: 16px 12px;
    overflow: auto;
  }
  .side-head-left {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .side-head,
  .transcript-head,
  .composer-actions,
  .new-actions,
  .head-actions {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }
  .eyebrow,
  .group-title,
  .field-label {
    color: var(--muted);
    font-size: var(--size-meta);
    line-height: 1.2;
    letter-spacing: 0.13em;
    text-transform: uppercase;
  }
  .collapse-button,
  .new-button,
  .destroy-button,
  .quiet-button,
  .send-button {
    height: 30px;
    padding: 0 12px;
    border: 1px solid var(--line-strong);
    font-size: var(--size-meta);
    line-height: 1;
  }
  .collapse-button,
  .new-button,
  .destroy-button,
  .quiet-button {
    color: var(--text);
    background: var(--panel-bg);
  }
  .collapse-button {
    width: 30px;
    padding: 0;
    background: transparent;
  }
  .destroy-button:hover {
    color: var(--err);
    border-color: var(--err-line);
    background: var(--err-bg);
  }
  .new-button:hover,
  .quiet-button:hover,
  .collapse-button:hover,
  .session-row:hover,
  .search-result:hover {
    background: var(--hover);
  }
  .session-row.chosen {
    background: var(--panel-bg);
    border-color: var(--line);
  }
  .search-label {
    position: relative;
    display: block;
    margin: 16px 0 12px;
  }
  .search-label input,
  .new-panel input,
  .new-panel textarea,
  .composer textarea,
  select {
    width: 100%;
    color: var(--text);
    background: var(--panel-bg);
    border: 1px solid var(--line-strong);
    padding: 0 9px;
    outline: none;
  }
  .search-label input,
  .new-panel input,
  select {
    height: 30px;
  }
  .new-panel textarea,
  .composer textarea {
    padding: 8px 10px;
  }
  input:focus,
  textarea:focus,
  select:focus {
    border-color: var(--info);
  }
  select {
    appearance: none;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath d='m2.5 4.5 3.5 3 3.5-3' fill='none' stroke='%2363605a' stroke-width='1.25'/%3E%3C/svg%3E");
    background-repeat: no-repeat;
    background-position: right 8px center;
    padding-right: 27px;
  }
  .search-pulse {
    position: absolute;
    top: 5px;
    right: 9px;
    color: var(--muted);
  }
  .group-title {
    display: flex;
    justify-content: space-between;
    margin: 12px 4px 6px;
  }
  .history-title {
    margin-top: 18px;
  }
  .history-toggle {
    margin: 8px 4px;
    padding: 2px 0;
    border: 0;
    background: none;
    color: var(--info);
    font-size: var(--size-meta);
  }
  .session-list {
    min-height: 0;
    display: grid;
    gap: 2px;
  }
  .session-row,
  .search-result {
    width: 100%;
    text-align: left;
    color: inherit;
    background: transparent;
    border: 1px solid transparent;
    padding: 7px 8px;
    display: flex;
    gap: 8px;
    align-items: flex-start;
    min-width: 0;
  }
  .dot {
    flex: 0 0 7px;
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: var(--dot-idle);
    margin-top: 6px;
  }
  .dot.running,
  .dot.working {
    background: var(--ok);
  }
  .dot.warn {
    background: var(--attn);
  }
  .dot.needs_input {
    background: var(--info);
  }
  .row-main {
    min-width: 0;
    flex: 1;
    display: grid;
    gap: 2px;
  }
  .session-name {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--text);
    font-size: 13px;
  }
  .row-sub,
  .row-cost,
  .result-meta,
  .session-context {
    color: var(--muted);
    font-size: var(--size-meta);
  }
  .row-cost {
    white-space: nowrap;
    margin-top: 1px;
  }
  .search-result {
    display: grid;
    gap: 4px;
  }
  .snippet {
    color: var(--text-soft);
    font-size: 13px;
    line-height: 1.35;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .transcript {
    min-width: 0;
    display: flex;
    flex-direction: column;
    min-height: 0;
    background: var(--panel-bg);
  }
  .transcript-head {
    padding: 14px 28px;
    border-bottom: 1px solid var(--line);
  }
  .head-main {
    min-width: 0;
  }
  .session-title {
    margin: 0;
    font-size: 15px;
    font-weight: 600;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .session-context {
    margin-top: 3px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .session-state {
    border-radius: 999px;
    padding: 4px 10px;
    font-size: var(--size-meta);
    text-transform: lowercase;
    white-space: nowrap;
  }
  .vm-chip {
    border-radius: 999px;
    border: 1px solid var(--line-strong);
    padding: 3px 10px;
    font-family: var(--font-mono);
    font-size: var(--size-meta);
    color: var(--muted);
    white-space: nowrap;
  }
  .vm-chip.vm-awake {
    color: var(--ok);
    border-color: var(--ok);
  }
  .vm-chip.vm-asleep {
    color: var(--info);
    border-color: var(--info);
  }
  .session-state.warn {
    color: var(--attn);
    background: var(--attn-soft);
  }
  .session-state.needs_input {
    color: var(--info);
    background: var(--info-soft);
  }
  .session-state.running,
  .session-state.working {
    color: var(--ok);
    background: var(--ok-soft);
  }
  .turns {
    flex: 1;
    padding: 8px 28px 20px;
    overflow: auto;
  }
  .turns-inner {
    max-width: 860px;
    margin: 0 auto;
  }
  .turn {
    padding: 18px 0 14px;
    border-top: 1px solid var(--line);
  }
  .turn:first-child {
    border-top: 0;
  }
  .prompt {
    display: flex;
    gap: 10px;
    align-items: baseline;
    background: var(--page-bg);
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 10px 12px;
  }
  .prompt-text {
    color: var(--text);
    font-weight: 500;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    min-width: 0;
  }
  .role {
    color: var(--muted);
    font-size: var(--size-meta);
    text-transform: uppercase;
    letter-spacing: 0.08em;
    flex: 0 0 auto;
  }
  .steps {
    margin: 10px 0 0;
  }
  .steps summary {
    cursor: pointer;
    user-select: none;
    width: fit-content;
    color: var(--muted);
    font-size: var(--size-meta);
    font-family: var(--font-mono);
    border-radius: 4px;
  }
  .steps summary:hover {
    color: var(--text);
  }
  .step-list {
    margin: 8px 0 0;
    padding: 0 0 0 12px;
    border-left: 2px solid var(--line);
    list-style: none;
    display: grid;
    gap: 4px;
  }
  .step-list li {
    min-width: 0;
  }
  .step-verb {
    color: var(--text);
    font-weight: 600;
    font-family: var(--font-mono);
    font-size: var(--size-meta);
  }
  .step-detail {
    color: var(--muted);
    font-family: var(--font-mono);
    font-size: var(--size-meta);
    overflow-wrap: anywhere;
  }
  .step-earlier {
    color: var(--muted);
    font-family: var(--font-mono);
    font-size: var(--size-meta);
  }
  .live-steps {
    margin-top: 8px;
  }
  .live-line {
    display: flex;
    align-items: center;
    gap: 8px;
    margin: 12px 0 0;
    color: var(--ok);
    font-family: var(--font-mono);
    font-size: var(--size-body-mono);
    min-width: 0;
  }
  .live-line.quiet {
    color: var(--muted);
  }
  .live-dot {
    flex: 0 0 8px;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: currentColor;
  }
  .live-latest {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .result-md {
    margin-top: 10px;
    color: var(--text-soft);
    line-height: 1.6;
    overflow-wrap: anywhere;
  }
  .result-md :global(p) {
    margin: 0 0 10px;
  }
  .result-md :global(p:last-child) {
    margin-bottom: 0;
  }
  .result-md :global(h2),
  .result-md :global(h3) {
    color: var(--text);
    margin: 16px 0 6px;
    font-size: 15px;
  }
  .result-md :global(h3) {
    font-size: 14px;
  }
  .result-md :global(ul),
  .result-md :global(ol) {
    margin: 8px 0 10px;
    padding-left: 22px;
  }
  .result-md :global(li) {
    margin: 3px 0;
  }
  .result-md :global(code) {
    font-family: var(--font-mono);
    font-size: var(--size-body-mono);
    background: var(--code-bg);
    border-radius: 3px;
    padding: 1px 4px;
  }
  .result-md :global(pre) {
    background: var(--code-bg);
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 10px 12px;
    overflow-x: auto;
    margin: 10px 0;
  }
  .result-md :global(pre code) {
    background: none;
    padding: 0;
  }
  .result-md :global(a) {
    color: var(--info);
  }
  .result-md :global(blockquote) {
    border-left: 3px solid var(--line-strong);
    margin: 10px 0;
    padding: 2px 12px;
    color: var(--muted);
  }
  .result-md :global(table) {
    border-collapse: collapse;
    margin: 10px 0;
  }
  .result-md :global(th),
  .result-md :global(td) {
    border: 1px solid var(--line);
    padding: 5px 9px;
    text-align: left;
  }
  .turn-error {
    margin: 10px 0 0;
    padding: 10px 12px;
    background: var(--err-bg);
    border: 1px solid var(--err-line);
    border-radius: 8px;
    color: var(--err);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    line-height: 1.5;
  }
  .turn-meta {
    display: flex;
    gap: 12px;
    margin-top: 10px;
    color: var(--muted);
    font-size: var(--size-meta);
  }
  .badge-failed {
    color: var(--err);
    font-weight: 600;
  }
  .composer {
    border-top: 1px solid var(--line);
    padding: 12px 28px 16px;
  }
  .composer-inner {
    max-width: 860px;
    margin: 0 auto;
    display: grid;
    gap: 10px;
  }
  .composer textarea {
    resize: vertical;
    min-height: 70px;
  }
  .composer-actions {
    align-items: flex-start;
  }
  .model-chips {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
  .chip {
    font-family: var(--font-mono);
    font-size: var(--size-meta);
    padding: 5px 10px;
    border: 1px solid var(--line-strong);
    border-radius: 999px;
    background: var(--panel-bg);
    color: var(--text);
  }
  .chip:hover {
    background: var(--hover);
  }
  .chip.on {
    background: var(--ink);
    border-color: var(--ink);
    color: var(--ink-text);
  }
  .send-button {
    color: var(--ink-text);
    background: var(--ink);
    border-color: var(--ink);
    font-weight: 600;
  }
  .send-button:disabled {
    cursor: not-allowed;
    opacity: 0.45;
  }
  .new-panel {
    position: fixed;
    z-index: 2;
    top: 0;
    right: 0;
    width: min(360px, 100vw);
    height: 100vh;
    overflow: auto;
    background: var(--panel-bg);
    border-left: 1px solid var(--line-strong);
    padding: 20px;
    box-shadow: -2px 0 12px rgba(0, 0, 0, 0.07);
  }
  .new-panel form {
    display: grid;
    gap: 14px;
    margin-top: 16px;
  }
  .new-panel label,
  .new-panel .field {
    display: grid;
    gap: 6px;
    color: var(--muted);
    font-size: var(--size-meta);
    text-transform: uppercase;
    letter-spacing: 0.08em;
  }
  .new-panel textarea {
    resize: vertical;
    color: var(--text);
    text-transform: none;
    letter-spacing: normal;
    font-size: var(--size-body);
  }
  .new-actions {
    justify-content: flex-end;
    margin-top: 4px;
  }
  .empty {
    padding: 10px 4px;
    color: var(--muted);
    font-size: 13px;
  }
  .blank-state {
    margin: auto;
  }
  .transcript-empty {
    padding: 32px 0;
  }
  .error-banner {
    position: fixed;
    right: 16px;
    bottom: 16px;
    max-width: 420px;
    padding: 10px 12px;
    border: 1px solid var(--err-line);
    border-radius: 6px;
    color: var(--err);
    background: var(--err-bg);
    font-size: 13px;
  }
  @media (prefers-reduced-motion: no-preference) {
    .dot.working,
    .live-dot {
      animation: pulse 1.2s ease-in-out infinite;
    }
  }
  .sidebar-collapsed {
    grid-template-columns: 44px minmax(0, 1fr);
  }
  :global(html[data-agents-rail="collapsed"]) .console {
    grid-template-columns: 44px minmax(0, 1fr);
  }
  .sidebar-collapsed .sidebar {
    padding: 12px 7px;
  }
  :global(html[data-agents-rail="collapsed"]) .console .sidebar {
    padding: 12px 7px;
  }
  .sidebar-collapsed .side-head {
    justify-content: center;
  }
  :global(html[data-agents-rail="collapsed"]) .console .side-head {
    justify-content: center;
  }
  .sidebar-collapsed .eyebrow,
  .sidebar-collapsed .new-button,
  .sidebar-collapsed .search-label,
  .sidebar-collapsed .group-title,
  .sidebar-collapsed .history-toggle,
  .sidebar-collapsed .row-main,
  .sidebar-collapsed .row-cost,
  .sidebar-collapsed .search-result,
  .sidebar-collapsed .empty {
    display: none;
  }
  :global(html[data-agents-rail="collapsed"]) .console .eyebrow,
  :global(html[data-agents-rail="collapsed"]) .console .new-button,
  :global(html[data-agents-rail="collapsed"]) .console .search-label,
  :global(html[data-agents-rail="collapsed"]) .console .group-title,
  :global(html[data-agents-rail="collapsed"]) .console .history-toggle,
  :global(html[data-agents-rail="collapsed"]) .console .row-main,
  :global(html[data-agents-rail="collapsed"]) .console .row-cost,
  :global(html[data-agents-rail="collapsed"]) .console .search-result,
  :global(html[data-agents-rail="collapsed"]) .console .empty {
    display: none;
  }
  .sidebar-collapsed .session-list {
    gap: 6px;
  }
  .sidebar-collapsed .session-row {
    justify-content: center;
    padding: 7px 0;
  }
  :global(html[data-agents-rail="collapsed"]) .console .session-list {
    gap: 6px;
  }
  :global(html[data-agents-rail="collapsed"]) .console .session-row {
    justify-content: center;
    padding: 7px 0;
  }
  .sidebar-collapsed .dot {
    margin-top: 5px;
  }
  @keyframes pulse {
    50% {
      opacity: 0.35;
    }
  }
  @media (max-width: 760px) {
    .console {
      grid-template-columns: 1fr;
    }
    .sidebar {
      max-height: 42vh;
      border-right: 0;
      border-bottom: 1px solid var(--line);
    }
    .sidebar-collapsed {
      grid-template-columns: 1fr;
    }
    .sidebar-collapsed .sidebar {
      max-height: none;
      border-bottom: 0;
    }
    .transcript-head,
    .turns,
    .composer {
      padding-left: 16px;
      padding-right: 16px;
    }
  }
  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
  }
</style>
