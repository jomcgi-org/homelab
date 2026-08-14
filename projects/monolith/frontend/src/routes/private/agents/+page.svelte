<script>
  import { onMount, tick } from "svelte";
  import { goto } from "$app/navigation";
  import { page } from "$app/stores";
  import { renderAgentMarkdown } from "./markdown.js";
  import { RUN_FIXTURES } from "./run-fixtures.js";
  // groupSessions and its helpers grouped run-spawned sessions into the
  // session list. Runs are their own collection now and their sessions live
  // inside the run, so the grouping is gone rather than hidden.
  import {
    backToRun,
    clearSelection,
    selectRun as runSearchTransition,
    selectSession as sessionTransition,
    withSearch,
  } from "./url-state.js";
  import { statusClass, statusLabel, vmRunning, vmState } from "./status.js";
  import "./agents-theme.css";
  import "./run-view.css";
  import RunView from "./RunView.svelte";
  import MasterView from "./MasterView.svelte";
  import StateIcon from "./StateIcon.svelte";
  import { nodeIconKey, nodeStateClass } from "./dag.js";
  import { firstLine, fmtCost, joinMeta } from "./run-format.js";
  import { partitionRuns, recentRuns, runActivityAt } from "./run-history.js";
  import { crumbTrail, sessionLineage } from "./lineage.js";
  import { RUN_LEXICON as P } from "./run-lexicon.js";
  import PaneHeader from "./PaneHeader.svelte";

  const MOBILE_MEDIA_QUERY = "(max-width: 760px)";

  let { data } = $props();

  const MODELS = ["opus", "fable", "sonnet", "luna", "terra", "sol", "qwen"];
  // Sidebar RECENT is a 24-hour navigation convenience. MasterView's activity
  // summary intentionally uses a separate seven-day window.
  const RECENT_HISTORY_MS = 24 * 60 * 60 * 1000;
  const ATTENTION_MS = 60 * 60 * 1000;
  // hasOwn, not a bare index: ?fixture=constructor would otherwise resolve to
  // Object.prototype.constructor, which is truthy, and the preview branch would
  // then hand RunView an undefined view and throw.
  const fixture = $derived.by(() => {
    const name = $page.url.searchParams.get("fixture");
    return name && Object.hasOwn(RUN_FIXTURES, name)
      ? RUN_FIXTURES[name]
      : undefined;
  });

  const selectedId = $derived($page.url.searchParams.get("session"));
  const selectedRunId = $derived($page.url.searchParams.get("run"));
  const mobileTranscript = $derived(
    isMobileViewport() && (selectedId != null || selectedRunId != null),
  );
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
  let runs = $state([]);
  let showTerminalHistory = $state(false);
  let terminalRuns = $state([]);
  let runMaster = $state({ runs: [], queues: [] });
  let masterView = $state({
    engine_tier: "live",
    snapshot_age_seconds: 0,
  });
  let masterSnapshotFetchedAt = 0;
  let masterHasSnapshot = false;
  let runDetail = $state(null);
  let runRequestSequence = 0;
  let detail = $state(null);
  let searchQuery = $state("");
  let searchResults = $state(null);
  let searchLoading = $state(false);
  let prompt = $state("");
  let composerModelOverride = $state(null);
  let sending = $state(false);
  let creating = $state(false);
  let showNewPanel = $state(false);
  let newPanelMode = $state("session");
  let newButtonEl = $state(null);
  let newPromptEl = $state(null);
  let titleEl = $state(null);
  let focusSessionId = null;
  let previousSessionId = null;
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
  let newRun = $state({ budget: "", idempotencyKey: "" });
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
  // The composer defaults to the selected session's model until the picker
  // overrides it. Derived rather than assigned in the selection effect: that
  // effect must not read `sessions`, or every 2s poll would re-run it and
  // wipe the open transcript.
  const composerModel = $derived(
    composerModelOverride ?? selectedSession?.model ?? "",
  );
  const hasRuns = $derived(runs.length > 0);
  const runNeedsAttention = $derived(runs.filter((run) => run.needs).length);
  const runSpend = $derived(
    runs.reduce((total, run) => total + Number(run.cost_usd || 0), 0),
  );
  const activeSessions = $derived(
    sessions.filter((session) => isActive(session)).sort(compareSessions),
  );

  // Runs and sessions are two collections, not one list with runs bolted on.
  // A session a run spawned is a detail of that run, reachable through it,
  // never a peer of a session you started yourself. Previously a run appeared
  // twice, once in an aggregate row and once as a group, and its children sat
  // at top level alongside standalone sessions.
  const isStandalone = (session) =>
    session?.workflow_id == null || session.workflow_id === "";
  const standaloneActive = $derived(activeSessions.filter(isStandalone));
  // Runs needing a human sort first; the engine's order holds within each half.
  const sidebarRuns = $derived(
    [...runs].sort(
      (a, b) => Number(Boolean(b.needs)) - Number(Boolean(a.needs)),
    ),
  );
  const recentTerminalRuns = $derived(recentRuns(terminalRuns));
  const visibleTerminalRuns = $derived(
    showTerminalHistory ? terminalRuns : recentTerminalRuns,
  );
  const historySessions = $derived(
    sessions.filter((session) => !isActive(session)).sort(compareSessions),
  );
  const recentHistorySessions = $derived(
    historySessions.filter(
      (session) => Date.now() - lastActiveAt(session) < RECENT_HISTORY_MS,
    ),
  );
  const masterActivity = $derived([
    ...runs.map((run) => ({ kind: "run", value: run, cost: run.cost_usd })),
    ...terminalRuns.map((run) => ({
      kind: "run",
      value: run,
      cost: run.cost_usd,
    })),
    ...sessions.filter(isStandalone).map((session) => ({
      kind: "session",
      value: session,
      cost: session.total_cost_usd,
    })),
  ]);
  const visibleHistorySessions = $derived(
    showAllHistory ? historySessions : recentHistorySessions,
  );
  const standaloneHistory = $derived(
    visibleHistorySessions.filter(isStandalone),
  );
  const standaloneHistoryTotal = $derived(
    historySessions.filter(isStandalone).length,
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
  const crumbRunTitle = $derived(
    selectedRunId ? firstLine(runDetail?.run?.task?.text) : "",
  );
  const crumbLineage = $derived(
    selectedRunId ? sessionLineage(runDetail?.run, selectedId) : null,
  );
  const sessionCrumbs = $derived(
    crumbTrail({
      kind: "session",
      runTitle: crumbRunTitle,
      nodeLabel: crumbLineage?.nodeLabel,
      attemptN: crumbLineage?.attemptN,
      sessionTitle: headerTitle,
    }),
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

  // Success allowlist shared with the backend's _CLEAN_TERMINAL_REASONS:
  // "completed"/"end_turn" from the claude lane, "stop" from the pi
  // lane's raw stopReason. One deliberate difference: a missing
  // terminal_reason warns the SESSION server-side (transport died
  // mid-turn) but does not paint the turn card failed here, since the
  // card has no error text to show for it.
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
      await loadRuns();
      errorMessage = null;
      if (
        selectedId != null &&
        !sessions.some((session) => String(session.id) === String(selectedId))
      ) {
        replaceWith(sessionTransition($page.url.searchParams, null));
      }
    } catch (error) {
      errorMessage = error.message;
    }
  }

  async function loadRuns() {
    if (fixture) return;
    try {
      const responses = await Promise.all([
        fetch("/agents/runs?active=true"),
        fetch("/agents/runs?active=false"),
      ]);
      if (responses.some((response) => !response.ok)) {
        throw new Error("swarm runs unavailable");
      }
      const [activeBody, terminalBody] = await Promise.all(
        responses.map((response) => response.json()),
      );
      runMaster = activeBody.master ?? activeBody;
      const activePartition = partitionRuns(runMaster.runs ?? []);
      const terminalPartition = partitionRuns(
        (terminalBody.master ?? terminalBody).runs ?? [],
      );
      runs = activePartition.inFlight;
      terminalRuns = terminalPartition.terminal;
      masterSnapshotFetchedAt = Date.now();
      masterHasSnapshot = true;
      masterView = { engine_tier: "live", snapshot_age_seconds: 0 };
    } catch {
      masterView = masterHasSnapshot
        ? {
            engine_tier: "stale",
            snapshot_age_seconds: Math.max(
              1,
              Math.floor((Date.now() - masterSnapshotFetchedAt) / 1000),
            ),
          }
        : { engine_tier: "absent", snapshot_age_seconds: 0 };
    }
  }

  async function loadRunDetail(id, sequence = runRequestSequence) {
    if (fixture) return;
    if (id == null) return;
    try {
      const response = await fetch(`/agents/runs/${encodeURIComponent(id)}`);
      if (!response.ok) throw new Error("swarm run unavailable");
      const body = await response.json();
      if (
        sequence === runRequestSequence &&
        String(selectedRunId) === String(id)
      ) {
        runDetail = {
          ...body,
          run: body.run ?? body,
          view: body.view ?? {
            engine_tier: "live",
            now: new Date().toISOString(),
            snapshot_age_seconds: 0,
          },
          sessions: (body.sessions ?? sessions).filter(
            (session) => String(session.workflow_id) === String(id),
          ),
        };
      }
    } catch {
      if (
        sequence === runRequestSequence &&
        String(selectedRunId) === String(id)
      ) {
        if (runDetail?.run) {
          runDetail = {
            ...runDetail,
            view: {
              ...runDetail.view,
              engine_tier: "stale",
              snapshot_age_seconds: Math.max(
                1,
                Number(runDetail.view?.snapshot_age_seconds || 0) + 1,
              ),
            },
          };
        } else {
          runDetail = {
            run: null,
            sessions: sessions.filter(
              (session) => String(session.workflow_id) === String(id),
            ),
            view: {
              engine_tier: "absent",
              now: new Date().toISOString(),
              snapshot_age_seconds: null,
            },
          };
        }
      }
    }
  }

  // `/private/agents` is the internal route id, not the address the browser is
  // on: the reroute hook (src/hooks.js) maps private.jomcgi.dev/agents onto it,
  // so the tier reaches this page at /agents. Pushing the internal path put a
  // URL in the address bar that nobody types or shares, and it only failed
  // quietly because the reroute guard skips paths already under /private/.
  // Keep whichever path the browser actually arrived on.
  //
  // goto, not pushState/replaceState from $app/navigation. Shallow routing
  // moves the address bar and sets page.state, but it never reassigns
  // page.url (only update_url() does, on a real navigation or popstate), so
  // every selection derived from page.url.searchParams recomputed to the
  // same string and the transcript pane never loaded. Deep links worked
  // because the server load set page.url; clicks did not. Do not "optimise"
  // this back. Semgrep no-shallow-routing-for-url-state guards it.
  //
  // noScroll keeps the transcript where it was, keepFocus is load-bearing
  // for the mobile drill below, which hand-manages focus after selection.
  function navigateTo(search) {
    goto(withSearch($page.url.pathname, search), {
      noScroll: true,
      keepFocus: true,
    });
  }

  // Correcting the URL for a selection that no longer exists, so it must not
  // add a history entry the back button has to walk back through.
  function replaceWith(search) {
    goto(withSearch($page.url.pathname, search), {
      replaceState: true,
      noScroll: true,
      keepFocus: true,
    });
  }

  function selectRun(runOrId) {
    const id = typeof runOrId === "object" ? runOrId?.workflow_id : runOrId;
    if (id == null) return;
    navigateTo(runSearchTransition($page.url.searchParams, id));
  }

  function selectRuns() {
    navigateTo("");
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
    navigateTo(sessionTransition($page.url.searchParams, id));
  }

  function isMobileViewport() {
    return (
      typeof window !== "undefined" &&
      window.matchMedia(MOBILE_MEDIA_QUERY).matches
    );
  }

  function returnToSessionList() {
    focusSessionId = selectedId;
    navigateTo(clearSelection($page.url.searchParams));
  }

  function returnToRun() {
    navigateTo(backToRun($page.url.searchParams));
  }

  function paneCrumb(to) {
    if (to === "home") selectRuns();
    else if (to === "run") returnToRun();
  }

  function closeNewPanel() {
    showNewPanel = false;
    tick().then(() => newButtonEl?.focus({ preventScroll: true }));
  }

  function openNewPanel(mode = "session") {
    newPanelMode = mode;
    newRun.idempotencyKey = crypto.randomUUID();
    showNewPanel = true;
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
      closeNewPanel();
      newSession = { prompt: "", model: "", repo: "", branch: "" };
      branches = [];
      await loadSessions();
      selectSession(body.session_id);
    } catch (error) {
      errorMessage = error.message;
      creating = false;
    }
  }

  async function createRun() {
    if (
      !newSession.prompt.trim() ||
      !newSession.repo ||
      !newSession.branch ||
      creating
    )
      return;
    if (
      newRun.budget !== "" &&
      (!Number.isFinite(Number(newRun.budget)) || Number(newRun.budget) <= 0)
    ) {
      errorMessage = P.labels.budgetPositive;
      return;
    }
    creating = true;
    errorMessage = null;
    try {
      const requestBody = {
        task: newSession.prompt.trim(),
        repo: newSession.repo,
        branch: newSession.branch,
        idempotency_key: newRun.idempotencyKey,
      };
      if (newRun.budget !== "") requestBody.budget_usd = Number(newRun.budget);
      const response = await fetch("/agents/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(requestBody),
      });
      const body = await response.json();
      // detail first: the swarm endpoint raises FastAPI HTTPExceptions, which
      // serialise as {detail}, so the useful reasons ("unknown repo x",
      // "budget_usd must be positive") live there and never in {error}.
      if (!response.ok)
        throw new Error(body.detail || body.error || P.labels.runCreateFailed);
      creating = false;
      closeNewPanel();
      newSession = { prompt: "", model: "", repo: "", branch: "" };
      newRun = { budget: "", idempotencyKey: "" };
      branches = [];
      await loadRuns();
      selectRun(body.workflow_id);
    } catch (error) {
      errorMessage = error.message;
      creating = false;
    }
  }

  async function destroySession() {
    if (!selectedId || !window.confirm(P.labels.destroyConfirm)) return;
    try {
      const response = await fetch(
        `/agents/session/${encodeURIComponent(selectedId)}`,
        { method: "DELETE" },
      );
      if (!response.ok) throw new Error("Session could not be destroyed");
      focusSessionId = selectedId;
      replaceWith(sessionTransition($page.url.searchParams, null));
      await loadSessions();
    } catch (error) {
      errorMessage = error.message;
    }
  }

  async function cancelRun(id) {
    if (!id || !window.confirm(P.labels.cancelRunConfirm)) return;
    try {
      const response = await fetch(
        `/agents/runs/${encodeURIComponent(id)}/cancel`,
        { method: "POST" },
      );
      if (!response.ok) throw new Error("Run could not be cancelled");
      await Promise.all([loadRuns(), loadRunDetail(id, runRequestSequence)]);
    } catch (error) {
      errorMessage = error.message;
    }
  }

  $effect(() => {
    if (showNewPanel) tick().then(() => newPromptEl?.focus());
  });

  onMount(() => {
    loadRuns();
    const handleKeydown = (event) => {
      if (event.key === "Escape" && !event.isComposing && showNewPanel) {
        event.preventDefault();
        closeNewPanel();
      }
    };
    window.addEventListener("keydown", handleKeydown);
    return () => {
      window.removeEventListener("keydown", handleKeydown);
    };
  });

  $effect(() => {
    const sessionId = selectedId;
    const runId = selectedRunId;
    const wasSessionId = previousSessionId;
    previousSessionId = sessionId;

    requestSequence += 1;
    runRequestSequence += 1;
    detail = null;
    runDetail = null;
    renderedPending = {};
    searchResults = null;
    composerModelOverride = null;

    if (runId != null) loadRunDetail(runId, runRequestSequence);
    if (sessionId != null) loadDetail(sessionId, requestSequence);

    // Every tier, not just mobile. goto keeps focus rather than dropping it
    // to <body>, so without this the ring stays on the sidebar row you
    // clicked while the pane beside it is what actually changed. Moving it
    // to the title announces the session to a screen reader and puts the
    // focus mark on the content you navigated to. titleEl is tabindex="-1"
    // and renders from the already-loaded session row, so it exists by the
    // time tick() resolves, before the detail fetch returns.
    if (sessionId != null) {
      tick().then(() => titleEl?.focus({ preventScroll: true }));
    } else if (
      isMobileViewport() &&
      runId == null &&
      sessionId == null &&
      wasSessionId != null
    ) {
      const rowId = focusSessionId ?? wasSessionId;
      tick().then(() =>
        document
          .getElementById(`agent-session-${String(rowId)}`)
          ?.focus({ preventScroll: true }),
      );
    }
  });

  $effect(() => {
    searchQuery;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(runSearch, 200);
    return () => clearTimeout(searchTimer);
  });

  $effect(() => {
    selectedId;
    selectedRunId;
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
      if (selectedRunId != null)
        await loadRunDetail(selectedRunId, runRequestSequence);
      if (selectedId != null)
        await loadDetail(selectedId, requestSequence, true);
    }, pollInterval);
    return () => clearInterval(interval);
  });

  $effect(() => {
    if (
      !fixture &&
      ((!selectedId && !selectedRunId) || showNewPanel) &&
      !reposLoaded &&
      !repoLoading
    ) {
      loadRepos();
    }
  });

  // Subscribe to the shared VM state stream. A slow poll is only a fallback
  // after the stream has failed repeatedly.
  $effect(() => {
    // The literal /private path, matching private/chat, NOT the short
    // /agents form the other fetches here use. The /private prefix is added
    // by the reroute hook inside this Node process, long after the gateway
    // has picked an HTTPRoute rule, so a short path reaches Envoy as
    // /agents/... , matches the catch-all, and never gets the long timeout
    // the stream needs. Envoy then resets it at its 15s default and
    // EventSource reconnects forever, roughly four times a minute.
    const source = new EventSource("/private/agents/vms/stream");
    let failures = 0;
    let fallbackInterval;

    const startFallback = () => {
      if (fallbackInterval) return;
      fallbackInterval = setInterval(loadVms, 2000);
      loadVms();
    };

    source.onmessage = (event) => {
      failures = 0;
      clearInterval(fallbackInterval);
      fallbackInterval = undefined;
      try {
        vms = JSON.parse(event.data).vms ?? {};
      } catch {
        // Ignore malformed advisory state and retain the last snapshot.
      }
    };
    source.onerror = () => {
      failures += 1;
      // A fatal error (non-200, or a content type that is not
      // text/event-stream, which is what an expired Cloudflare Access
      // session returns) closes the source permanently and fires exactly
      // ONE error. Counting to three never gets there, so the case the
      // fallback exists for was the one case it could not cover.
      if (source.readyState === EventSource.CLOSED || failures >= 3) {
        startFallback();
      }
    };

    return () => {
      source.close();
      clearInterval(fallbackInterval);
    };
  });

  $effect(() => () => {
    clearTimeout(searchTimer);
    searchController?.abort();
  });
</script>

<svelte:head><title>{P.labels.pageTitle}</title></svelte:head>

<main
  class:sidebar-collapsed={sidebarCollapsed}
  class:mobile-transcript={mobileTranscript}
  class="console"
>
  <aside class="sidebar" aria-label={P.labels.sessionsRegion}>
    <div class="side-head">
      <div class="side-head-left">
        <div class="eyebrow">{P.labels.sessionsEyebrow}</div>
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
        bind:this={newButtonEl}
        onclick={() => (showNewPanel ? closeNewPanel() : openNewPanel())}
        >+ new</button
      >
    </div>

    <label class="search-label">
      <span class="sr-only">{P.labels.searchLabel}</span>
      <input
        bind:value={searchQuery}
        placeholder={P.labels.searchPlaceholder}
        autocomplete="off"
      />
      {#if searchLoading}<span class="search-pulse">…</span>{/if}
    </label>

    {#if searchResults !== null}
      <div class="group-title">{P.labels.searchResults}</div>
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
        {:else}<div class="empty">{P.labels.noMatchingTurns}</div>{/each}
      </div>
    {:else}
      <!-- The heading is the affordance for the swarm home: clicking it
           clears the selection. There is no aggregate pseudo-row, which is
           what made a run appear twice. -->
      <div class="group-title">
        <button class="section-link" type="button" onclick={selectRuns}
          >{P.labels.runsWord}</button
        ><span
          >{#if runNeedsAttention}<span class="needs-tag"
              >{runNeedsAttention} {P.labels.needsYou}</span
            >{:else}{sidebarRuns.length}{/if}</span
        >
      </div>
      <div class="session-list runs-list">
        {#each sidebarRuns as run (run.workflow_id)}
          {@render runRow(run)}
        {:else}<div class="empty">{P.labels.noneYet}</div>{/each}
      </div>
      <div class="group-title history-title">
        {P.labels.earlierRuns} <span>{terminalRuns.length}</span>
      </div>
      <div class="session-list runs-list">
        {#each visibleTerminalRuns as run (run.workflow_id)}
          {@render terminalRunRow(run)}
        {:else}<div class="empty">{P.labels.noEarlierRuns}</div>{/each}
      </div>
      {#if terminalRuns.length > visibleTerminalRuns.length}
        <button
          class="history-toggle"
          type="button"
          onclick={() => (showTerminalHistory = !showTerminalHistory)}
          >{showTerminalHistory
            ? P.labels.showRecentOnly
            : `${P.labels.showAll} (${terminalRuns.length})`}</button
        >
      {/if}
      {#if standaloneActive.length}
        <div class="group-title">
          Active <span>{standaloneActive.length}</span>
        </div>
        <div class="session-list">
          {#each standaloneActive as session (session.id)}
            {@render sessionRow(session)}
          {/each}
        </div>
      {/if}
      <div class="group-title history-title">
        {P.labels.sessionsSection}
      </div>
      <div class="session-list">
        {#each standaloneHistory as session (session.id)}
          {@render sessionRow(session)}
        {:else}<div class="empty">
            {standaloneActive.length
              ? P.labels.noRecentSessions
              : P.labels.noSessionsYet}
          </div>{/each}
      </div>
      {#if standaloneHistoryTotal > standaloneHistory.length}
        <button
          class="history-toggle"
          type="button"
          onclick={() => (showAllHistory = !showAllHistory)}
          >{showAllHistory
            ? "show recent only"
            : `show all (${standaloneHistoryTotal})`}</button
        >
      {/if}
    {/if}
  </aside>

  <section class="transcript" aria-label={P.labels.transcriptRegion}>
    <button
      class="mobile-back"
      type="button"
      aria-label={P.labels.backToSessions}
      onclick={returnToSessionList}>{P.labels.mobileBack}</button
    >
    {#if fixture && !fixture.home}
      <RunView
        run={fixture.run}
        view={fixture.view}
        sessions={fixture.sessions}
        onSelectSession={selectSession}
        onCrumb={paneCrumb}
      />
    {:else if selectedId && selectedSession}
      <header class="transcript-head">
        <PaneHeader
          kind={P.labels.sessionWord}
          crumbs={sessionCrumbs}
          onCrumb={paneCrumb}
        >
          {#snippet chips()}
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
            <!-- One right-hand group whether or not the session came from a
                 run. Pushing only the back link would leave destroy sitting
                 against the state chip on a standalone session, so the
                 button a mis-click destroys a VM with moves under the
                 cursor depending on how you arrived. -->
            <span class="push head-right">
              {#if selectedRunId}
                <button class="back-to-run" type="button" onclick={returnToRun}
                  >{P.labels.backToRun}</button
                >
              {/if}
              <button
                class="destroy-button"
                type="button"
                onclick={destroySession}>{P.labels.destroy}</button
              >
            </span>
          {/snippet}
        </PaneHeader>
        <h1
          class="session-title"
          title={headerTitle}
          tabindex="-1"
          bind:this={titleEl}
        >
          {headerTitle}
        </h1>
        <div class="session-context mono">
          {formatRepoContext(selectedSession)} · {selectedSession.model ||
            "luna"} · {shortId(selectedSession)}
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
                {#if turnFailed(turn)}<span class="badge-failed"
                    >{P.labels.turnFailed}</span
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
                  {:else if partial?.partial_text}
                    <span class="live-latest">{P.labels.working}</span>
                  {:else if vmRunning(selectedSession, vms)}
                    <!-- Claimed, VM confirmed running, no output yet: the
                         CLI is spinning up / the model has the prompt. -->
                    <span class="live-latest">{P.labels.startingAgent}</span>
                  {:else}
                    <!-- Claimed but the control plane does not report the
                         guest running yet: park rejoin or cold boot. -->
                    <span class="live-latest">{P.labels.wakingVm}</span>
                  {/if}
                {:else if state === "starting"}
                  <span class="live-latest">{P.labels.startingUp}</span>
                {:else}
                  <span class="live-latest">{P.labels.waitingForTurn}</span>
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
                : P.labels.loadingSession}
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
              composerModelOverride = model;
            })}
            <button
              class="send-button"
              type="submit"
              disabled={sending || !prompt.trim()}
              >{sending ? P.labels.sending : P.labels.send}</button
            >
          </div>
        </div>
      </form>
    {:else if selectedId}
      <!-- ?session= for a row absent from the server-rendered list. Without
           this branch the pane falls through to the master view and swaps
           once loadDetail resolves. -->
      <div class="loading-session">
        <PaneHeader kind={P.labels.sessionWord} />
        <div class="empty blank-state">{P.labels.loadingSession}</div>
      </div>
    {:else if selectedRunId}
      {#if runDetail?.run}
        <RunView
          run={runDetail.run}
          view={runDetail.view}
          sessions={runDetail.sessions}
          onSelectSession={selectSession}
          onCancel={() => cancelRun(selectedRunId)}
          onCrumb={paneCrumb}
        />
        <!-- loadRunDetail's catch leaves runDetail set with run: null and the
             tier marked absent. Without this branch that state is
             indistinguishable from the pre-fetch one, so a ?run= naming a run
             that does not resolve sits on "Loading run…" forever and reads as
             broken navigation rather than a missing run. -->
      {:else if runDetail?.view?.engine_tier === "absent"}
        <div class="empty blank-state">{P.labels.absentNotice}</div>
      {:else}<div class="empty blank-state">{P.labels.loadingRun}</div>{/if}
    {:else}
      <MasterView
        master={fixture?.home ? fixture.master : runMaster}
        activity={fixture?.home ? fixture.activity : masterActivity}
        sessions={fixture?.home ? fixture.sessions : sessions}
        {newSession}
        {newRun}
        {repos}
        {branches}
        {repoLoading}
        {branchLoading}
        {modelPicker}
        onChangeSession={(field, value) => (newSession[field] = value)}
        onChangeRun={(field, value) => (newRun[field] = value)}
        onLoadBranches={loadBranches}
        onCreateSession={createSession}
        onCreateRun={createRun}
        onSelectRun={selectRun}
        onSelectSession={selectSession}
        {relativeTime}
        onStartRun={() => openNewPanel("run")}
        view={fixture?.home ? fixture.view : masterView}
      />
    {/if}
  </section>

  {#if showNewPanel}
    <button
      class="new-panel-scrim"
      type="button"
      aria-label="Close new session panel"
      onclick={closeNewPanel}
    ></button>
    <section
      class="new-panel"
      role="dialog"
      aria-label={newPanelMode === "run"
        ? P.labels.newRun
        : P.labels.newSession}
    >
      <div class="eyebrow">
        {newPanelMode === "run" ? P.labels.newRun : P.labels.newSession}
      </div>
      <div class="field mode-field">
        <span class="field-label">{P.labels.mode}</span>
        <select bind:value={newPanelMode}>
          <option value="session">{P.labels.sessionMode}</option>
          <option value="run">{P.labels.runMode}</option>
        </select>
      </div>
      <form
        onsubmit={(event) => {
          event.preventDefault();
          if (newPanelMode === "run") createRun();
          else createSession();
        }}
      >
        <label
          >{newPanelMode === "run" ? P.labels.task : "prompt"}<textarea
            bind:value={newSession.prompt}
            bind:this={newPromptEl}
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
                if (newPanelMode === "run") createRun();
                else createSession();
              }
            }}></textarea></label
        >
        {#if newPanelMode === "session"}
          <div class="field">
            <span class="field-label">{P.labels.modelWord}</span>
            {@render modelPicker(newSession.model, (model) => {
              newSession.model = model;
            })}
          </div>
        {/if}
        <label
          >{P.labels.repoWord}<select
            class="mono"
            bind:value={newSession.repo}
            required={newPanelMode === "run"}
            disabled={repoLoading}
            onchange={() => {
              newSession.branch = "";
              loadBranches(newSession.repo);
            }}
          >
            {#if repoLoading}
              <option value="">{P.labels.loadingRepos}</option>
            {:else}
              {#if newPanelMode === "session"}
                <option value="">{P.labels.scratchWorkspace}</option>
              {:else}
                <option value="">{P.labels.selectRepo}</option>
              {/if}
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
            required={newPanelMode === "run"}
          >
            {#if branchLoading}
              <option value="">{P.labels.loadingBranches}</option>
            {:else if branches.length === 0}
              <option value="main">main</option>
            {:else}
              {#each branches as branch}
                <option value={branch.name}>{branch.name}</option>
              {/each}
            {/if}
          </select>
        </label>
        {#if newPanelMode === "run"}
          <label
            >{P.labels.budgetWord}<input
              type="number"
              min="0"
              step="0.01"
              bind:value={newRun.budget}
            /></label
          >
        {/if}
        <div class="new-actions">
          <button type="button" class="quiet-button" onclick={closeNewPanel}
            >{P.labels.cancelWord}</button
          ><button
            class="send-button"
            type="submit"
            disabled={creating ||
              !newSession.prompt.trim() ||
              (newPanelMode === "run" &&
                (!newSession.repo || !newSession.branch))}
            >{creating
              ? newPanelMode === "run"
                ? P.labels.startingRun
                : P.labels.creating
              : newPanelMode === "run"
                ? P.labels.createRun
                : P.labels.create}</button
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
    id={`agent-session-${String(session.id)}`}
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

{#snippet terminalRunRow(run)}
  <button
    class="group-header group-main run-entry run-row"
    class:chosen={String(selectedRunId) === String(run.workflow_id)}
    type="button"
    title={run.workflow_id}
    onclick={() => selectRun(run.workflow_id)}
  >
    <StateIcon
      icon={nodeIconKey({ state: run.state })}
      class={nodeStateClass({ state: run.state })}
    />
    <span class="group-run-title">{firstLine(run.title)}</span>
    <span class="group-run-meta mono"
      >{joinMeta(
        P.stateWords[run.state] || run.state,
        fmtCost(run.cost_usd),
        relativeTime(runActivityAt(run)),
      )}</span
    >
  </button>
{/snippet}

{#snippet runRow(run)}
  <button
    class="group-header group-main run-entry run-row"
    class:chosen={String(selectedRunId) === String(run.workflow_id)}
    type="button"
    title={run.workflow_id}
    onclick={() => selectRun(run.workflow_id)}
  >
    <!-- The shape strip is the run's state at a glance and the only thing
         that has to stay legible at 44px, which is what the collapsed rail
         renders. -->
    <span class="ic-strip run-shape-strip" aria-hidden="true">
      {#each run.shape ?? [] as node (node.key)}
        <StateIcon icon={nodeIconKey(node)} class={nodeStateClass(node)} />
      {/each}
    </span>
    <span class="group-run-title">{firstLine(run.title)}</span>
    <span class="group-run-meta mono"
      >{joinMeta(
        P.stateWords[run.state] || run.state,
        fmtCost(run.cost_usd),
      )}</span
    >
    {#if run.needs}<span class="needs-tag">{P.labels.needsYou}</span>{/if}
  </button>
{/snippet}

{#snippet modelPicker(current, choose)}
  <div class="model-chips" role="group" aria-label="Model">
    <button
      type="button"
      class="chip"
      class:on={!current}
      aria-pressed={!current}
      onclick={() => choose("")}>{P.labels.defaultWord}</button
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
  <label class="model-select-label">
    <span class="sr-only">Model</span>
    <select
      class="mono"
      aria-label="Model"
      value={current}
      onchange={(event) => choose(event.currentTarget.value)}
    >
      <option value="">{P.labels.defaultWord}</option>
      {#if current && !MODELS.includes(current)}
        <option value={current}>{current}</option>
      {/if}
      {#each MODELS as model}<option value={model}>{model}</option>{/each}
    </select>
  </label>
{/snippet}

<style>
  :global(*) {
    box-sizing: border-box;
  }
  :global(body) {
    margin: 0;
  }

  .console {
    color-scheme: light;
    height: 100dvh;
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
    border-radius: var(--radius-lg);
  }
  button:focus-visible,
  input:focus-visible,
  textarea:focus-visible,
  select:focus-visible,
  .steps summary:focus-visible {
    outline: 4px solid var(--info);
    outline-offset: 4px;
  }
  .sidebar {
    min-height: 0;
    border-right: 4px solid var(--line);
    padding: 16px 12px;
    overflow: auto;
  }
  .side-head-left {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .side-head,
  .composer-actions,
  .new-actions {
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
    border: 4px solid var(--line-strong);
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
  .search-result:hover,
  .group-header:hover {
    background: var(--hover);
  }
  .session-row.chosen {
    background: var(--panel-bg);
    border-color: var(--line);
  }
  .run-row.chosen {
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
    border: 4px solid var(--line-strong);
    padding: 0 8px;
    outline: none;
  }
  .search-label input,
  .new-panel input,
  select {
    height: 30px;
  }
  .new-panel textarea,
  .composer textarea {
    padding: 8px 8px;
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
    top: 4px;
    right: 8px;
    color: var(--muted);
  }
  .group-title {
    display: flex;
    justify-content: space-between;
    margin: 12px 4px 8px;
  }
  /* The runs heading is a button (clicking it clears the selection and
     returns to the swarm home) sitting beside the plain-text sessions
     heading. Without this reset it kept the UA button chrome, a bordered
     lowercase pill, and the two collection headings read as different
     kinds of thing. Every other button in this console carries an explicit
     reset for the same reason; see .session-row. Inherit rather than
     restate the .group-title type so the two headings cannot drift apart. */
  .section-link {
    padding: 0;
    border: 0;
    background: none;
    color: inherit;
    font: inherit;
    letter-spacing: inherit;
    text-transform: inherit;
    text-align: left;
  }
  .section-link:hover {
    color: var(--text);
  }
  .section-link:focus-visible {
    outline: none;
    text-decoration: underline;
    text-underline-offset: 4px;
  }
  .history-title {
    margin-top: 16px;
  }
  .history-toggle {
    margin: 8px 4px;
    padding: 4px 0;
    border: 0;
    background: none;
    color: var(--info);
    font-size: var(--size-meta);
  }
  .session-list {
    min-height: 0;
    display: grid;
    gap: 4px;
  }
  .session-group {
    min-width: 0;
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
  }
  .group-header {
    min-width: 0;
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 4px 8px;
    border: 4px solid transparent;
    color: var(--muted);
    background: transparent;
    text-align: left;
    font-size: var(--size-meta);
    white-space: nowrap;
  }
  .group-main {
    width: 100%;
  }
  .group-chevron {
    width: auto;
    padding-right: 8px;
    padding-left: 8px;
  }
  .group-id,
  .group-summary {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .run-shape-strip {
    width: auto;
    height: 1.05em;
    display: inline-flex;
    align-items: center;
    gap: 4px;
    overflow: hidden;
  }
  .group-run-title {
    min-width: 0;
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--text);
  }
  .group-run-meta {
    flex: 0 0 auto;
    color: var(--text-soft);
    text-align: right;
  }
  .group-id {
    flex: 0 0 auto;
  }
  .group-summary {
    min-width: 0;
    flex: 1;
  }
  .group-toggle {
    flex: 0 0 auto;
  }
  .group-members {
    grid-column: 1 / -1;
    padding-left: 8px;
  }
  .session-row,
  .search-result {
    width: 100%;
    text-align: left;
    color: inherit;
    background: transparent;
    border: 4px solid transparent;
    padding: 8px 8px;
    display: flex;
    gap: 8px;
    align-items: flex-start;
    min-width: 0;
  }
  .dot {
    flex: 0 0 8px;
    width: 8px;
    height: 8px;
    border-radius: var(--radius-circle);
    background: var(--dot-idle);
    margin-top: 8px;
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
    gap: 4px;
  }
  .session-name {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--text);
    font-size: var(--size-detail);
  }
  .row-sub,
  .row-cost,
  .result-meta,
  .session-context {
    color: var(--text-soft);
    font-size: var(--size-meta);
  }
  .back-to-run {
    display: inline-block;
    margin-bottom: 4px;
    padding: 0;
    border: 0;
    background: none;
    color: var(--muted);
    font: inherit;
    font-size: var(--size-meta);
    cursor: pointer;
  }
  .back-to-run:hover {
    color: var(--text);
    text-decoration: underline;
  }
  .row-cost {
    white-space: nowrap;
    margin-top: 4px;
  }
  .search-result {
    display: grid;
    gap: 4px;
  }
  .snippet {
    color: var(--text-soft);
    font-size: var(--size-detail);
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
    display: block;
    padding: 12px 28px;
    border-bottom: 4px solid var(--line);
  }
  .head-right {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .session-title {
    margin: 0;
    font-size: var(--size-title);
    font-weight: 600;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .session-title:focus:not(:focus-visible) {
    outline: none;
  }
  .session-context {
    margin-top: 4px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .session-state {
    border-radius: var(--radius-pill);
    padding: 4px 8px;
    font-size: var(--size-meta);
    text-transform: lowercase;
    white-space: nowrap;
  }
  /* Soft tinted fills matching the session-state pills: state reads from
     color, not from a hard outline. */
  .vm-chip {
    border-radius: var(--radius-pill);
    padding: 4px 8px;
    font-family: var(--font-mono);
    font-size: var(--size-meta);
    color: var(--muted);
    background: var(--hover);
    white-space: nowrap;
  }
  .vm-chip.vm-awake {
    color: var(--ok);
    background: var(--ok-soft);
  }
  .vm-chip.vm-asleep {
    color: var(--info);
    background: var(--info-soft);
  }
  .session-state.warn {
    color: var(--attn-text);
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
    padding: 16px 0 12px;
    border-top: 4px solid var(--line);
  }
  .turn:first-child {
    border-top: 0;
  }
  .prompt {
    display: flex;
    gap: 8px;
    align-items: baseline;
    background: var(--page-bg);
    border: 4px solid var(--line);
    border-radius: var(--radius-lg);
    padding: 8px 12px;
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
    margin: 8px 0 0;
  }
  .steps summary {
    cursor: pointer;
    user-select: none;
    width: fit-content;
    color: var(--muted);
    font-size: var(--size-meta);
    font-family: var(--font-mono);
    border-radius: var(--radius-md);
  }
  .steps summary:hover {
    color: var(--text);
  }
  .step-list {
    margin: 8px 0 0;
    padding: 0 0 0 12px;
    border-left: 4px solid var(--line);
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
    border-radius: var(--radius-circle);
    background: currentColor;
  }
  .live-latest {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .result-md {
    margin-top: 8px;
    color: var(--text-soft);
    line-height: 1.6;
    overflow-wrap: anywhere;
  }
  .result-md :global(p) {
    margin: 0 0 8px;
  }
  .result-md :global(p:last-child) {
    margin-bottom: 0;
  }
  .result-md :global(h2),
  .result-md :global(h3) {
    color: var(--text);
    margin: 16px 0 8px;
    font-size: var(--size-title);
  }
  .result-md :global(h3) {
    font-size: var(--size-body);
  }
  .result-md :global(ul),
  .result-md :global(ol) {
    margin: 8px 0 8px;
    padding-left: 22px;
  }
  .result-md :global(li) {
    margin: 4px 0;
  }
  .result-md :global(code) {
    font-family: var(--font-mono);
    font-size: var(--size-body-mono);
    background: var(--code-bg);
    border-radius: var(--radius-sm);
    padding: 4px 4px;
  }
  .result-md :global(pre) {
    background: var(--code-bg);
    border: 4px solid var(--line);
    border-radius: var(--radius-lg);
    padding: 8px 12px;
    overflow-x: auto;
    margin: 8px 0;
  }
  .result-md :global(pre code) {
    background: none;
    padding: 0;
  }
  .result-md :global(a) {
    color: var(--info);
  }
  .result-md :global(blockquote) {
    border-left: 4px solid var(--line-strong);
    margin: 8px 0;
    padding: 4px 12px;
    color: var(--muted);
  }
  .result-md :global(table) {
    border-collapse: collapse;
    margin: 8px 0;
  }
  .result-md :global(th),
  .result-md :global(td) {
    border: 4px solid var(--line);
    padding: 4px 8px;
    text-align: left;
  }
  .turn-error {
    margin: 8px 0 0;
    padding: 8px 12px;
    background: var(--err-bg);
    border: 4px solid var(--err-line);
    border-radius: var(--radius-lg);
    color: var(--err);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    line-height: 1.5;
  }
  .turn-meta {
    display: flex;
    gap: 12px;
    margin-top: 8px;
    color: var(--text-soft);
    font-size: var(--size-meta);
  }
  .badge-failed {
    color: var(--err);
    font-weight: 600;
  }
  .composer {
    border-top: 4px solid var(--line);
    padding: 12px 28px 16px;
  }
  .composer-inner {
    max-width: 860px;
    margin: 0 auto;
    display: grid;
    gap: 8px;
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
    gap: 8px;
  }
  .chip {
    font-family: var(--font-mono);
    font-size: var(--size-meta);
    padding: 4px 8px;
    border: 4px solid var(--line-strong);
    border-radius: var(--radius-pill);
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
    height: 100dvh;
    overflow: auto;
    background: var(--panel-bg);
    border-left: 4px solid var(--line-strong);
    padding: 20px;
    box-shadow: -4px 0 12px rgba(0, 0, 0, 0.07);
  }
  .new-panel-scrim,
  .mobile-back,
  .model-select-label {
    display: none;
  }
  .new-panel form {
    display: grid;
    gap: 12px;
    margin-top: 16px;
  }
  .new-panel label,
  .new-panel .field {
    display: grid;
    gap: 8px;
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
    padding: 8px 4px;
    color: var(--muted);
    font-size: var(--size-detail);
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
    padding: 8px 12px;
    border: 4px solid var(--err-line);
    border-radius: var(--radius-lg);
    color: var(--err);
    background: var(--err-bg);
    font-size: var(--size-detail);
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
    padding: 12px 8px;
  }
  :global(html[data-agents-rail="collapsed"]) .console .sidebar {
    padding: 12px 8px;
  }
  .sidebar-collapsed .side-head {
    justify-content: center;
  }
  :global(html[data-agents-rail="collapsed"]) .console .side-head {
    justify-content: center;
  }
  /* The rail declares what it shows instead of listing what to remove.
     Subtraction is why "…633" appeared stacked above bare dots: the hide list
     named the elements that existed when it was written, so a row gaining a
     title or a short id later leaked into 44px as a truncated fragment. A run
     is its shape strip, a session is its dot, and anything else inside a row
     is hidden by construction, including things not yet invented. */
  .sidebar-collapsed .session-list button > *:not(.run-shape-strip):not(.dot) {
    display: none;
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
  /* Same rule for the pre-hydration rail the static shell stamps on <html>,
     so the collapsed state does not flash a different shape before hydration. */
  :global(html[data-agents-rail="collapsed"])
    .console
    .session-list
    button
    > *:not(.run-shape-strip):not(.dot) {
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
    gap: 8px;
  }
  .sidebar-collapsed .session-row {
    justify-content: center;
    padding: 8px 0;
  }
  :global(html[data-agents-rail="collapsed"]) .console .session-list {
    gap: 8px;
  }
  :global(html[data-agents-rail="collapsed"]) .console .session-row {
    justify-content: center;
    padding: 8px 0;
  }
  .sidebar-collapsed .dot {
    margin-top: 4px;
  }
  @keyframes pulse {
    50% {
      opacity: 0.35;
    }
  }
  /* Matches MOBILE_MEDIA_QUERY at the top of this file */
  @media (max-width: 760px) {
    .console {
      grid-template-columns: 1fr;
    }
    .sidebar {
      border-right: 0;
      border-bottom: 0;
    }
    .console.mobile-transcript .sidebar {
      display: none;
    }
    .console:not(.mobile-transcript) .transcript {
      display: none;
    }
    .mobile-back {
      display: block;
      min-height: 44px;
      padding: 0 8px;
      border: 0;
      color: var(--info);
      background: transparent;
    }
    .transcript-head {
      padding: 12px 16px;
    }
    .new-panel-scrim {
      display: block;
      position: fixed;
      z-index: 2;
      inset: 0;
      width: 100%;
      min-height: 100%;
      padding: 0;
      border: 0;
      border-radius: 0;
      background: var(--scrim-bg);
    }
    .new-panel {
      z-index: 3;
      left: 0;
      right: 0;
      bottom: 0;
      top: auto;
      width: 100%;
      height: auto;
      max-height: 85dvh;
      overflow: auto;
      border-left: 0;
      border-top: 4px solid var(--line-strong);
      border-radius: 8px 8px 0 0;
    }
    .model-chips {
      display: none;
    }
    .model-select-label {
      display: block;
      flex: 1;
    }
    .model-select-label select {
      min-height: 44px;
    }
    .collapse-button,
    .new-button,
    .destroy-button,
    .quiet-button,
    .send-button {
      min-height: 44px;
    }
    .collapse-button {
      min-width: 44px;
    }
    .steps summary {
      min-height: 44px;
      display: inline-flex;
      align-items: center;
    }
    .session-row {
      min-height: 56px;
    }
    .search-label input,
    .new-panel select,
    .history-toggle,
    .search-result {
      min-height: 44px;
    }
    .sidebar-collapsed {
      grid-template-columns: 1fr;
    }
    .sidebar-collapsed .sidebar {
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
    width: 4px;
    height: 4px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
  }
</style>
