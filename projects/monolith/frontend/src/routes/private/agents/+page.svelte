<script>
  let { data } = $props();

  const MODELS = ["opus", "fable", "luna", "terra", "sol", "qwen"];

  let selectedId = $state(null);
  let sessions = $state(data.sessions ?? []);
  let detail = $state(null);
  let searchQuery = $state("");
  let searchResults = $state(null);
  let searchLoading = $state(false);
  let prompt = $state("");
  let composerModel = $state("luna");
  let sending = $state(false);
  let creating = $state(false);
  let newSession = $state({
    prompt: "",
    model: "luna",
    workspace: "",
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
  const selectedIsPending = $derived((selectedSession?.pending_count ?? 0) > 0);
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

  function cost(value) {
    return `$${Number(value || 0).toFixed(4)}`;
  }

  function statusClass(session) {
    if (isActive(session)) return "working";
    if (["error", "failed", "failure"].includes(session?.status))
      return "error";
    return "idle";
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

  async function loadDetail(id, sequence = requestSequence) {
    if (id == null) return;
    try {
      const response = await fetch(`/agents/session/${encodeURIComponent(id)}`);
      if (!response.ok) throw new Error("Unable to load session");
      const body = await response.json();
      if (sequence === requestSequence && String(selectedId) === String(id)) {
        detail = body;
        if (body.session?.model) composerModel = body.session.model;
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

  function selectSession(sessionOrId) {
    const id = typeof sessionOrId === "object" ? sessionOrId?.id : sessionOrId;
    if (id == null) return;
    requestSequence += 1;
    selectedId = id;
    detail = null;
    searchResults = null;
    loadDetail(id, requestSequence);
    const session = sessions.find((item) => String(item.id) === String(id));
    composerModel = session?.model || "luna";
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

  function onSearchInput() {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(runSearch, 200);
  }

  async function sendPrompt() {
    if (!selectedId || !prompt.trim() || sending) return;
    sending = true;
    errorMessage = null;
    try {
      const response = await fetch(
        `/agents/session/${encodeURIComponent(selectedId)}/messages`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ prompt: prompt.trim(), model: composerModel }),
        },
      );
      const body = await response.json();
      if (!response.ok || body.accepted === false)
        throw new Error(body.error || "Message was not accepted");
      prompt = "";
      await Promise.all([
        loadSessions(),
        loadDetail(selectedId, requestSequence),
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
      const response = await fetch("/agents/sessions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          prompt: newSession.prompt.trim(),
          model: newSession.model,
          workspace: newSession.workspace.trim(),
          branch: newSession.branch.trim(),
        }),
      });
      const body = await response.json();
      if (!response.ok || body.accepted === false)
        throw new Error(body.error || "Session was not created");
      creating = false;
      newSession = { prompt: "", model: "luna", workspace: "", branch: "" };
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
      if (sessions[0]) selectSession(sessions[0]);
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
        if (selectedId != null) await loadDetail(selectedId, requestSequence);
      },
      hasActiveSessions ? 2000 : 15000,
    );
    return () => clearInterval(interval);
  });

  $effect(() => () => {
    clearTimeout(searchTimer);
    searchController?.abort();
  });
</script>

<svelte:head><title>Agents</title></svelte:head>

<main class="console">
  <aside class="sidebar">
    <div class="side-head">
      <div class="eyebrow">agent sessions</div>
      <button
        class="new-button"
        type="button"
        onclick={() => (creating = !creating)}>+ new</button
      >
    </div>

    <label class="search-label">
      <span class="sr-only">Search sessions</span>
      <input
        bind:value={searchQuery}
        oninput={onSearchInput}
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
            <span class="result-id"
              >{result.local_session_id ||
                `${result.workspace}/${result.seq}`}</span
            >
            <span class="snippet">{result.snippet}</span>
            <span class="result-meta"
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
          <div class="eyebrow">{displayName(selectedSession)}</div>
          <div class="session-context">
            {selectedSession.workspace} / {selectedSession.branch}
          </div>
        </div>
        <div class="head-actions">
          <span class:working={selectedIsPending} class="session-state"
            >{selectedSession.status}</span
          >
          <button class="destroy-button" type="button" onclick={destroySession}
            >destroy</button
          >
        </div>
      </header>
      <div class="turns">
        {#each detail?.turns ?? [] as turn (turn.seq)}
          <article class="turn">
            <div class="turn-bar">
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
              <span class="queue-seq">#{entry.seq}</span>
              <span class="queue-prompt">{entry.prompt}</span>
              <span class:claimed={entry.claimed_by_replica} class="claim"
                >{entry.claimed_by_replica ? "claimed" : "waiting"}</span
              >
              <span class="muted">{relativeTime(entry.created_at)}</span>
            </div>
          {/each}
        {/if}
      </div>

      {#if selectedIsPending}<div class="working-line">
          <span class="dot working"></span> worker is processing the queue
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
          rows="3"></textarea>
        <div class="composer-actions">
          <select bind:value={composerModel} aria-label="Model">
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

  {#if creating}
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
            placeholder="what should the agent do?"></textarea></label
        >
        <label
          >model<select bind:value={newSession.model}
            >{#each MODELS as model}<option value={model}>{model}</option
              >{/each}</select
          ></label
        >
        <label
          >workspace<input
            bind:value={newSession.workspace}
            placeholder="workspace"
          /></label
        >
        <label
          >branch<input
            bind:value={newSession.branch}
            placeholder="branch"
          /></label
        >
        <div class="new-actions">
          <button
            type="button"
            class="quiet-button"
            onclick={() => (creating = false)}>cancel</button
          ><button
            class="send-button"
            type="submit"
            disabled={creating && !newSession.prompt.trim()}
            >{creating ? "create" : "create"}</button
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
    onclick={() => selectSession(session)}
  >
    <span
      class={`dot ${statusClass(session)}`}
      aria-label={statusClass(session)}
    ></span>
    <span class="row-main"
      ><span class="session-name">{displayName(session)}</span><span
        class="row-sub"
        >{session.model || "luna"} · {relativeTime(
          session.last_turn_at || session.created_at,
        )}</span
      ></span
    >
    <span class="row-cost">{cost(session.total_cost_usd)}</span>
  </button>
{/snippet}

<style>
  :global(*) {
    box-sizing: border-box;
  }
  :global(body) {
    margin: 0;
    background: #101214;
    color: #d7dbd8;
    font-family:
      ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  }
  :global(button),
  :global(input),
  :global(textarea),
  :global(select) {
    font: inherit;
  }
  :global(button) {
    cursor: pointer;
  }
  .console {
    min-height: 100vh;
    display: grid;
    grid-template-columns: 300px minmax(0, 1fr);
    background: #101214;
  }
  .sidebar {
    border-right: 1px solid #2a2e30;
    padding: 24px 14px;
    overflow: auto;
  }
  .side-head,
  .transcript-head,
  .composer-actions,
  .new-actions,
  .head-actions {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }
  .eyebrow,
  .group-title,
  .queue-title {
    color: #929a96;
    font-size: 11px;
    letter-spacing: 0.13em;
    text-transform: uppercase;
  }
  .new-button,
  .destroy-button,
  .quiet-button {
    color: #aeb6b2;
    background: transparent;
    border: 1px solid #3a403e;
    padding: 5px 8px;
    font-size: 11px;
  }
  .new-button:hover,
  .destroy-button:hover,
  .quiet-button:hover {
    color: #e0e5e1;
    border-color: #737b76;
  }
  .search-label {
    position: relative;
    display: block;
    margin: 22px 0 20px;
  }
  .search-label input,
  .new-panel input,
  .new-panel textarea,
  .composer textarea,
  select {
    width: 100%;
    color: #d7dbd8;
    background: #171a1c;
    border: 1px solid #343a38;
    border-radius: 2px;
    padding: 9px 10px;
    outline: none;
  }
  input:focus,
  textarea:focus,
  select:focus {
    border-color: #88918b;
  }
  .search-pulse {
    position: absolute;
    top: 8px;
    right: 9px;
    color: #737c77;
  }
  .group-title {
    display: flex;
    justify-content: space-between;
    margin: 14px 5px 7px;
  }
  .history-title {
    margin-top: 28px;
  }
  .session-list {
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
    padding: 9px 7px;
    display: flex;
    gap: 9px;
    align-items: flex-start;
    min-width: 0;
  }
  .session-row:hover,
  .session-row.chosen,
  .search-result:hover {
    background: #1b1f20;
    border-color: #2d3331;
  }
  .dot {
    flex: 0 0 7px;
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: #747b77;
    margin-top: 5px;
  }
  .dot.working {
    background: #a9b5ae;
  }
  .dot.error {
    background: #b87171;
  }
  .row-main {
    min-width: 0;
    flex: 1;
    display: grid;
    gap: 4px;
  }
  .session-name,
  .result-id {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: #d7dbd8;
    font-size: 12px;
  }
  .row-sub,
  .row-cost,
  .result-meta,
  .muted,
  .session-context {
    color: #7d8681;
    font-size: 10px;
  }
  .row-cost {
    white-space: nowrap;
  }
  .search-result {
    display: grid;
    gap: 5px;
  }
  .snippet {
    color: #aeb6b2;
    font-size: 11px;
    line-height: 1.4;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .transcript {
    min-width: 0;
    display: flex;
    flex-direction: column;
    min-height: 100vh;
  }
  .transcript-head {
    padding: 24px 30px 18px;
    border-bottom: 1px solid #2a2e30;
  }
  .session-context {
    margin-top: 7px;
  }
  .session-state {
    color: #8d9691;
    border: 1px solid #303634;
    padding: 4px 7px;
    font-size: 10px;
  }
  .turns {
    flex: 1;
    padding: 18px 30px;
    overflow: auto;
  }
  .turn {
    border: 1px solid #2b302e;
    margin-bottom: 14px;
    background: #141719;
  }
  .turn-bar {
    display: flex;
    gap: 14px;
    padding: 8px 11px;
    border-bottom: 1px solid #292e2c;
    color: #818a85;
    font-size: 10px;
  }
  .turn-bar span:nth-last-child(2) {
    margin-left: auto;
  }
  .prompt {
    padding: 13px 13px 9px;
    color: #d7dbd8;
    white-space: pre-wrap;
    line-height: 1.5;
    font-size: 13px;
  }
  .role {
    color: #8c9891;
    margin-right: 10px;
    font-size: 10px;
    text-transform: uppercase;
  }
  .activities {
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
    padding: 0 13px 12px;
  }
  code {
    color: #aab4ad;
    border: 1px solid #303735;
    background: #191d1e;
    padding: 3px 5px;
    font-size: 10px;
    white-space: pre-wrap;
  }
  .result {
    margin: 0;
    padding: 13px;
    border-top: 1px solid #292e2c;
    color: #bfc7c2;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    font: inherit;
    font-size: 12px;
    line-height: 1.6;
  }
  .queue-title {
    margin: 26px 0 8px;
  }
  .queue-entry {
    display: grid;
    grid-template-columns: 38px minmax(0, 1fr) auto auto;
    align-items: center;
    gap: 10px;
    border-top: 1px solid #252a28;
    padding: 10px 3px;
    font-size: 11px;
  }
  .queue-prompt {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .queue-seq,
  .claim {
    color: #818a85;
  }
  .claim.claimed {
    color: #b7c0ba;
  }
  .working-line {
    border-top: 1px solid #2a2e30;
    padding: 10px 30px;
    color: #89938d;
    font-size: 11px;
  }
  .working-line .dot {
    display: inline-block;
    margin: 0 8px 1px 0;
  }
  .composer {
    border-top: 1px solid #2a2e30;
    padding: 16px 30px 22px;
    display: grid;
    gap: 9px;
  }
  .composer textarea {
    resize: vertical;
    min-height: 70px;
  }
  select {
    width: auto;
    min-width: 110px;
    padding: 7px 9px;
  }
  .send-button {
    color: #151817;
    background: #b6c0b9;
    border: 1px solid #b6c0b9;
    padding: 7px 12px;
    font-size: 11px;
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
    background: #171a1c;
    border-left: 1px solid #3a403e;
    padding: 28px 22px;
    box-shadow: -12px 0 30px #0005;
  }
  .new-panel form {
    display: grid;
    gap: 15px;
    margin-top: 24px;
  }
  .new-panel label {
    display: grid;
    gap: 7px;
    color: #929a96;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
  }
  .new-panel textarea {
    resize: vertical;
  }
  .new-actions {
    justify-content: flex-end;
    margin-top: 8px;
  }
  .empty {
    padding: 12px 5px;
    color: #68716c;
    font-size: 11px;
  }
  .blank-state {
    margin: auto;
  }
  .transcript-empty {
    padding: 30px 0;
  }
  .error-banner {
    position: fixed;
    right: 18px;
    bottom: 18px;
    max-width: 420px;
    padding: 10px 12px;
    border: 1px solid #805858;
    color: #d3aaa7;
    background: #241a1b;
    font-size: 11px;
  }
  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
  }
  @media (max-width: 760px) {
    .console {
      grid-template-columns: 1fr;
    }
    .sidebar {
      max-height: 42vh;
      border-right: 0;
      border-bottom: 1px solid #2a2e30;
    }
    .transcript-head,
    .turns,
    .composer,
    .working-line {
      padding-left: 16px;
      padding-right: 16px;
    }
  }
</style>
