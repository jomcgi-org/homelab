<script>
  import { statusClass, statusLabel } from "./status.js";

  let { data } = $props();

  const MODELS = ["opus", "fable", "sonnet", "luna", "terra", "sol", "qwen"];

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
  let repos = $state([]);
  let branches = $state([]);
  let repoLoading = $state(false);
  let branchLoading = $state(false);
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
  const visibleSearchResults = $derived(searchResults ?? []);
  const hasActiveSessions = $derived(
    sessions.some((session) => isActive(session)) ||
      Number(
        sessions.find((session) => String(session.id) === String(selectedId))
          ?.pending_count,
      ) > 0,
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

  function displayName(session) {
    return (
      session?.local_session_id ||
      `${session?.workspace || "workspace"}/${session?.branch || "branch"}`
    );
  }

  function formatRepoContext(session) {
    if (session?.repo) return `${session.repo}@${session.branch || "main"}`;
    return `${session?.workspace || "workspace"} / ${session?.branch || "branch"}`;
  }

  function cost(value) {
    return `$${Number(value || 0).toFixed(4)}`;
  }

  function activityLabel(activity) {
    if (typeof activity === "string") return activity;
    if (!activity || typeof activity !== "object") return "activity";
    const tool = String(activity.tool || activity.name || "activity");
    if (tool.toLowerCase() === "edit" || tool.toLowerCase() === "write") {
      return `${tool} ${activity.file_path || activity.path || "file"}`;
    }
    if (tool.toLowerCase() === "bash" || tool.toLowerCase() === "shell") {
      return `${tool} ${activity.command || activity.input || ""}`.trim();
    }
    return tool;
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
    }
  }

  async function loadBranches(repoId) {
    if (!repoId) {
      branches = [];
      return;
    }
    branchLoading = true;
    const [owner, repo] = repoId.split("/");
    try {
      const response = await fetch(
        `/agents/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}/branches`,
      );
      if (!response.ok) throw new Error("Unable to load branches");
      const body = await response.json();
      branches = body.branches ?? [];
      newSession.branch = body.default_branch ?? "main";
    } catch (error) {
      branches = [];
      newSession.branch = "main";
    } finally {
      branchLoading = false;
    }
  }

  function selectSession(sessionOrId) {
    const id = typeof sessionOrId === "object" ? sessionOrId?.id : sessionOrId;
    if (id == null) return;
    requestSequence += 1;
    selectedId = id;
    detail = null;
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
      } else {
        requestBody.workspace = newSession.workspace?.trim() || "";
        requestBody.branch = newSession.branch?.trim() || "";
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
    const interval = setInterval(
      async () => {
        await loadSessions();
        if (selectedId != null)
          await loadDetail(selectedId, requestSequence, true);
      },
      hasActiveSessions ? 2000 : 15000,
    );
    return () => clearInterval(interval);
  });

  $effect(() => {
    if (showNewPanel && repos.length === 0) {
      loadRepos();
    }
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
        placeholder="search transcript"
        autocomplete="off"
      />
      {#if searchLoading}<span class="search-pulse">...</span>{/if}
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
            <span class="result-id mono"
              >{result.local_session_id ||
                `${result.workspace}/${result.seq}`}</span
            >
            <span class="snippet">{result.snippet}</span>
            <span class="result-meta mono"
              >turn {result.seq} · {relativeTime(result.created_at)}</span
            >
          </button>
        {:else}<div class="empty">No matching turns</div>{/each}
      </div>
    {:else}
      <div class="group-title">Active <span>{activeSessions.length}</span></div>
      <div class="session-list">
        {#each activeSessions as session (session.id)}
          {@render sessionRow(session)}
        {:else}<div class="empty">No active sessions</div>{/each}
      </div>
      <div class="group-title history-title">
        History <span>{historySessions.length}</span>
      </div>
      <div class="session-list">
        {#each historySessions as session (session.id)}
          {@render sessionRow(session)}
        {:else}<div class="empty">No history</div>{/each}
      </div>
    {/if}
  </aside>

  <section class="transcript" aria-label="Agent transcript">
    {#if selectedSession}
      <header class="transcript-head">
        <div>
          <div class="eyebrow mono">{displayName(selectedSession)}</div>
          <div class="session-context mono">
            {formatRepoContext(selectedSession)}
          </div>
        </div>
        <div class="head-actions">
          <span class={`session-state ${statusClass(selectedSession)}`}
            >{statusLabel(selectedSession)}</span
          >
          <button class="destroy-button" type="button" onclick={destroySession}
            >destroy</button
          >
        </div>
      </header>
      <div class="turns">
        {#each detail?.turns ?? [] as turn (turn.seq)}
          <article class="turn">
            <div class="turn-bar mono">
              <span>#{turn.seq}</span><span
                >{turn.model || selectedSession.model || "luna"}</span
              ><span>{cost(turn.cost_usd)}</span><span
                >{turn.stop_reason || turn.terminal_reason || "pending"}</span
              >
            </div>
            <div class="prompt"><span class="role">you</span>{turn.prompt}</div>
            {#if turn.usage?.activities?.length}
              <div class="activities" aria-label="Tool activity">
                {#each turn.usage.activities as activity}<code
                    >{activityLabel(activity)}</code
                  >{/each}
              </div>
            {/if}
            {#if turn.result_text}<pre
                class="result">{turn.result_text}</pre>{/if}
          </article>
        {:else}<div class="empty transcript-empty">
            No completed turns
          </div>{/each}

        {#if detail?.pending_queue?.length}
          <div class="queue-title">pending queue</div>
          {#each detail.pending_queue as entry (entry.seq)}
            <div class="queue-entry">
              <span class="queue-seq mono">#{entry.seq}</span>
              <span class="queue-prompt">{entry.prompt}</span>
              <span class:claimed={entry.claimed_by_replica} class="claim"
                >{entry.claimed_by_replica ? "claimed" : "waiting"}</span
              >
              <span class="muted mono">{relativeTime(entry.created_at)}</span>
            </div>
          {/each}
        {/if}
      </div>

      {#if detail?.pending_queue?.length}<div class="working-line shimmer">
          <span class="dot working"></span>
          {#if detail.pending_queue.some((entry) => entry.claimed_by_replica)}running{:else}waking…{/if}
          <span class="muted">{detail.pending_queue.length} queued</span>
        </div>{/if}
      <form
        class="composer"
        onsubmit={(event) => {
          event.preventDefault();
          sendPrompt();
        }}
      >
        <textarea
          bind:value={prompt}
          placeholder="send a prompt to this session"
          rows="3"
          onkeydown={(e) => {
            if (
              (e.metaKey || e.ctrlKey) &&
              e.key === "Enter" &&
              !sending &&
              prompt.trim()
            ) {
              e.preventDefault();
              sendPrompt();
            }
          }}></textarea>
        <div class="composer-actions">
          <select class="mono" bind:value={composerModel} aria-label="Model">
            <option value="">session default</option>
            {#each MODELS as model}<option value={model}>{model}</option>{/each}
          </select>
          <button
            class="send-button"
            type="submit"
            disabled={sending || !prompt.trim()}
            >{sending ? "sending..." : "send"}</button
          >
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
                !creating &&
                newSession.prompt.trim()
              ) {
                e.preventDefault();
                createSession();
              }
            }}></textarea></label
        >
        <label
          >model<select class="mono" bind:value={newSession.model}
            ><option value="">session default</option
            >{#each MODELS as model}<option value={model}>{model}</option
              >{/each}</select
          ></label
        >
        <label
          >repo<select
            class="mono"
            bind:value={newSession.repo}
            onchange={() => {
              newSession.branch = "";
              loadBranches(newSession.repo);
            }}
          >
            <option value="">none (bare workspace)</option>
            {#each repos as repo}
              <option value={repo.id} title={repo.description || ""}>
                {repo.id}
              </option>
            {/each}
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
              <option value="">main</option>
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
            >{creating ? "creating" : "create"}</button
          >
        </div>
      </form>
    </section>
  {/if}
</main>

{#if errorMessage}<div class="error-banner" role="status">
    {errorMessage}
  </div>{/if}

{#snippet sessionRow(session)}
  <button
    class:chosen={String(selectedId) === String(session.id)}
    class="session-row"
    type="button"
    aria-label={`${displayName(session)}: ${statusLabel(session)}`}
    onclick={() => selectSession(session)}
  >
    <span
      class={`dot ${statusClass(session)}`}
      title={`${displayName(session)}: ${statusLabel(session)}`}
    ></span>
    <span class="row-main"
      ><span class="session-name">{displayName(session)}</span><span
        class="row-sub mono"
      >
        {#if session.repo}{session.repo}@{session.branch ||
            "main"}{:else}{session.model || "luna"} · {relativeTime(
            session.last_turn_at || session.created_at,
          )}{/if}
      </span></span
    >
    <span class="row-cost mono">{cost(session.total_cost_usd)}</span>
  </button>
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
    --size-meta: 11px;
    --size-body-mono: 12.5px;
    --size-body: 13.5px;
    --page-bg: #f5f2ec;
    --panel-bg: #fffdfa;
    --text: #252521;
    --muted: #827c72;
    --line: #d8d2c7;
    --line-strong: #c9c2b7;
    color-scheme: light;
    height: 100vh;
    display: grid;
    grid-template-columns: 300px minmax(0, 1fr);
    background: var(--page-bg);
    color: var(--text);
    font-family: var(--font-ui);
    font-size: var(--size-body);
    line-height: 1.4;
  }
  .console * {
    font-family: var(--font-ui);
  }
  .console .mono,
  .console code,
  .console pre,
  .console input.mono,
  .console select.mono {
    font-family: var(--font-mono);
  }
  .mono,
  code,
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
    border-radius: 3px;
  }
  button:focus-visible,
  input:focus-visible,
  textarea:focus-visible,
  select:focus-visible {
    outline: 2px solid #8aa9c2;
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
  .queue-title {
    color: #77736b;
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
    padding: 0 10px;
    border: 1px solid var(--line-strong);
    border-radius: 3px;
    font-size: var(--size-meta);
    line-height: 1;
  }
  .collapse-button,
  .new-button,
  .destroy-button,
  .quiet-button {
    color: #4e4a43;
    background: transparent;
  }
  .collapse-button {
    width: 30px;
    padding: 0;
  }
  .new-button:hover,
  .destroy-button:hover,
  .quiet-button:hover,
  .collapse-button:hover,
  .session-row:hover,
  .session-row.chosen,
  .search-result:hover {
    background: #ebe7df;
  }
  .search-label {
    position: relative;
    display: block;
    margin: 16px 0;
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
    border-radius: 3px;
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
    padding: 7px 9px;
  }
  input:focus,
  textarea:focus,
  select:focus {
    border-color: #7794a8;
  }
  select {
    appearance: none;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath d='m2.5 4.5 3.5 3 3.5-3' fill='none' stroke='%236b665d' stroke-width='1.25'/%3E%3C/svg%3E");
    background-repeat: no-repeat;
    background-position: right 8px center;
    padding-right: 27px;
  }
  .search-pulse {
    position: absolute;
    top: 5px;
    right: 9px;
    color: #8b857b;
  }
  .group-title {
    display: flex;
    justify-content: space-between;
    margin: 12px 4px 6px;
  }
  .history-title {
    margin-top: 20px;
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
    padding: 7px 6px;
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
    background: #979188;
    margin-top: 5px;
  }
  .dot.running {
    background: #4c9660;
  }
  .dot.working {
    background: #4c9660;
    animation: pulse 1.2s ease-in-out infinite;
  }
  .dot.warn {
    background: #b47b2c;
  }
  .dot.needs_input {
    background: #4a83ad;
  }
  .row-main {
    min-width: 0;
    flex: 1;
    display: grid;
    gap: 2px;
  }
  .session-name,
  .result-id {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: #302e29;
    font-size: var(--size-body-mono);
  }
  .row-sub,
  .row-cost,
  .result-meta,
  .muted,
  .session-context {
    color: var(--muted);
    font-size: var(--size-meta);
  }
  .row-cost {
    white-space: nowrap;
  }
  .search-result {
    display: grid;
    gap: 4px;
  }
  .snippet {
    color: #625e56;
    font-size: var(--size-body);
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
  }
  .transcript-head {
    padding: 16px 24px 12px;
    border-bottom: 1px solid var(--line);
  }
  .session-context {
    margin-top: 4px;
    font-family: var(--font-mono);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .session-state {
    color: #625e56;
    border: 1px solid var(--line-strong);
    padding: 5px 8px;
    font-size: var(--size-meta);
    text-transform: lowercase;
  }
  .session-state.completed {
    color: #938d83;
  }
  .session-state.warn {
    color: #c79f4a;
  }
  .session-state.needs_input {
    color: #4a8ec7;
  }
  .session-state.running,
  .session-state.working {
    color: #4a9f5c;
  }
  .turns {
    flex: 1;
    padding: 16px 24px;
    overflow: auto;
  }
  .turn {
    border: 1px solid var(--line);
    margin-bottom: 12px;
    background: var(--panel-bg);
  }
  .turn-bar {
    display: flex;
    gap: 12px;
    padding: 7px 10px;
    border-bottom: 1px solid #e2ddd4;
    color: var(--muted);
    font-size: var(--size-meta);
  }
  .turn-bar span:nth-last-child(2) {
    margin-left: auto;
  }
  .prompt {
    padding: 10px 12px 8px;
    color: #302e29;
    white-space: pre-wrap;
    line-height: 1.45;
    font-size: var(--size-body);
  }
  .role {
    color: var(--muted);
    margin-right: 8px;
    font-size: var(--size-meta);
    text-transform: uppercase;
  }
  .activities {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    padding: 0 12px 10px;
  }
  code {
    color: #625e56;
    border: 1px solid var(--line);
    background: var(--page-bg);
    padding: 3px 5px;
    white-space: pre-wrap;
  }
  .result {
    margin: 0;
    padding: 12px;
    border-top: 1px solid #e2ddd4;
    color: #4d4942;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    line-height: 1.5;
  }
  .queue-title {
    margin: 20px 0 6px;
  }
  .queue-entry {
    display: grid;
    grid-template-columns: 38px minmax(0, 1fr) auto auto;
    align-items: center;
    gap: 8px;
    border-top: 1px solid #e2ddd4;
    padding: 8px 3px;
    font-size: var(--size-body-mono);
  }
  .queue-prompt {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .queue-seq,
  .claim {
    color: var(--muted);
  }
  .claim.claimed {
    color: #4c9660;
  }
  .working-line {
    border-top: 1px solid var(--line);
    padding: 8px 24px;
    color: #625e56;
    font-size: var(--size-meta);
  }
  .working-line.shimmer {
    background: linear-gradient(90deg, transparent, #ebe7df, transparent);
    background-size: 200% 100%;
    animation: shimmer 1.8s linear infinite;
  }
  .working-line .dot {
    display: inline-block;
    margin: 0 7px 1px 0;
  }
  .working-line .muted {
    margin-left: 6px;
  }
  .composer {
    border-top: 1px solid var(--line);
    padding: 12px 24px 16px;
    display: grid;
    gap: 8px;
  }
  .composer textarea {
    resize: vertical;
    min-height: 70px;
  }
  .composer select {
    width: auto;
    min-width: 110px;
  }
  .send-button {
    color: var(--panel-bg);
    background: #4d6757;
    border-color: #4d6757;
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
    width: min(350px, 100vw);
    height: 100vh;
    background: var(--page-bg);
    border-left: 1px solid var(--line-strong);
    padding: 20px;
    box-shadow: -1px 0 2px #4b463d1a;
  }
  .new-panel form {
    display: grid;
    gap: 12px;
    margin-top: 16px;
  }
  .new-panel label {
    display: grid;
    gap: 5px;
    color: #77736b;
    font-size: var(--size-meta);
    text-transform: uppercase;
    letter-spacing: 0.08em;
  }
  .new-panel textarea {
    resize: vertical;
  }
  .new-actions {
    justify-content: flex-end;
    margin-top: 4px;
  }
  .empty {
    padding: 10px 4px;
    color: #938d83;
    font-size: var(--size-meta);
  }
  .blank-state {
    margin: auto;
  }
  .transcript-empty {
    padding: 24px 0;
  }
  .error-banner {
    position: fixed;
    right: 16px;
    bottom: 16px;
    max-width: 420px;
    padding: 9px 11px;
    border: 1px solid #c58c88;
    color: #874b46;
    background: #fff1ef;
    font-size: var(--size-meta);
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
  @keyframes shimmer {
    from {
      background-position: 200% 0;
    }
    to {
      background-position: -200% 0;
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
    .composer,
    .working-line {
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
