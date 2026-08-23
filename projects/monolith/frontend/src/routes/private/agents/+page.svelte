<script>
  import { onMount, tick } from "svelte";
  import { goto } from "$app/navigation";
  import { page } from "$app/stores";
  import { RUN_FIXTURES } from "./run-fixtures.js";
  // groupSessions and its helpers grouped run-spawned sessions into the
  // session list. Runs are their own collection now and their sessions live
  // inside the run, so the grouping is gone rather than hidden.
  import {
    backToRun,
    clearSelection,
    selectRun as runSearchTransition,
    selectSession as sessionTransition,
    setVoiceMode,
    withSearch,
  } from "./url-state.js";
  import { statusClass, statusLabel, vmState } from "./status.js";
  import "./agents-theme.css";
  import "./run-view.css";
  import RunView from "./RunView.svelte";
  import Launcher from "./Launcher.svelte";
  import { shapeStateClass } from "./dag.js";
  import { firstLine, fmtCost } from "./run-format.js";
  import { partitionRuns, relativeTime } from "./run-history.js";
  import {
    arrivalSelection,
    runAsk,
    inboxGroups,
    jumpTotal,
    railState,
    recentSummary,
  } from "./inbox.js";
  import { crumbTrail, sessionLineage } from "./lineage.js";
  import { setupVisualViewport } from "./visual-viewport.js";
  import { nextStatus, streamAge } from "./vm-stream-status.js";
  import { RUN_LEXICON as P } from "./run-lexicon.js";
  import PaneHeader from "./PaneHeader.svelte";
  import WalkthroughNarrative from "./WalkthroughNarrative.svelte";
  import JumpPalette from "./JumpPalette.svelte";
  import Turns from "./Turns.svelte";
  import VoiceCompanion from "./VoiceCompanion.svelte";
  import {
    answerCard,
    applyLedgerRows,
    askWorkflowId,
    dismissCard,
    emptyStage,
    togglePinned,
  } from "./companion/stage.js";
  import { decidePoll } from "./companion/poll.js";
  import { sessionTitle } from "./jump.js";
  import {
    defaultSessionView,
    SESSION_VIEW_CONVERSATION,
    SESSION_VIEW_WALKTHROUGH,
    walkthroughTurns,
  } from "./session-view.js";
  import { periodForHour } from "$lib/private/period.js";

  const MOBILE_MEDIA_QUERY = "(max-width: 760px)";
  const VOICE_POLL_MS = 2000;
  const VOICE_STORAGE_ID = "voice-companion-id";

  let { data } = $props();

  const MODELS = ["opus", "fable", "sonnet", "luna", "terra", "sol", "qwen"];
  const DEV_MODELS = ["qwen"];
  // Retry logic for initial run load before concluding engine is absent.
  // A transient network failure on first load should not blank the page.
  const RUNS_LOAD_MAX_ATTEMPTS = 3;
  const RUNS_LOAD_BACKOFF_MS = 200;
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
  const voiceMode = $derived($page.url.searchParams.get("mode") === "voice");
  const mobileTranscript = $derived(
    isMobileViewport() &&
      (voiceMode || selectedId != null || selectedRunId != null),
  );
  let manualRail = $state(null);
  const voiceFold = $derived(voiceMode);
  let searchOpensRail = $state(false);
  // True only after /agents/runs has succeeded at least once. Before that the
  // inbox is empty for the wrong reason (not fetched, or the fetch failed), so
  // it must not count as a quiet day: no auto-fold, no override reset. This
  // makes first paint open-then-fold on a quiet day, chosen over
  // folded-then-open on a busy one.
  let runsLoaded = $state(Boolean(fixture));
  if (typeof window !== "undefined") {
    try {
      const storedRail = window.localStorage.getItem(
        "agents-sidebar-collapsed",
      );
      manualRail =
        storedRail === "folded" || storedRail === "open" ? storedRail : null;
    } catch (e) {
      // localStorage blocked; continue with automatic rail state
    }
  }
  let sessions = $state(data.sessions ?? []);
  let availableModels = $state(MODELS);
  let runs = $state([]);
  let terminalRuns = $state([]);
  let runDetail = $state(null);
  let runRequestSequence = 0;
  let detail = $state(null);
  let voiceStage = $state(emptyStage());
  let voiceRows = $state([]);
  let voiceCompanionId = $state(null);
  let voiceSince = $state(0);
  let voiceSessionDetail = $state(null);
  let voiceRunDetails = $state({});
  let voiceNow = $state(Date.now());
  let sessionView = $state(SESSION_VIEW_CONVERSATION);
  let sessionViewInitializedFor = $state(null);
  let searchQuery = $state("");
  let searchResults = $state(null);
  let searchLoading = $state(false);
  let jumpOpen = $state(false);
  let jumpQuery = $state("");
  let prompt = $state("");
  let composerModelOverride = $state(null);
  let sending = $state(false);
  let creating = $state(false);
  let needsInputState = $state(false);
  let pendingTaskId = $state(null);
  let showNewPanel = $state(false);
  let newButtonEl = $state(null);
  let newPromptEl = $state(null);
  let repoControlEl = $state(null);
  let titleEl = $state(null);
  // ── Period (time-of-day palette) ────────────
  let now = $state(new Date());
  let period = $derived(periodForHour(now.getHours()));

  $effect(() => {
    // Update the period attribute on document.documentElement to enable CSS
    // selectors that key off data-agents-period. This matches the initial
    // value set by the blocking inline script in app.html.
    if (typeof document !== "undefined") {
      document.documentElement.setAttribute("data-agents-period", period);
    }
  });

  onMount(() => {
    // Update the clock every 60 seconds to match the landing page's refresh rate.
    const id = setInterval(() => (now = new Date()), 60_000);
    return () => clearInterval(id);
  });

  let focusSessionId = null;
  let previousSessionId = null;
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
  let searchController = null;
  let requestSequence = 0;
  let renderedPending = $state({});
  let vms = $state({});
  let vmSnapshotReceived = $state(false);
  let vmStreamStatus = $state({
    mode: "connecting",
    lastUpdateAt: null,
    error: null,
  });
  let vmStreamNow = $state(Date.now());
  let vmFallbackArmed = false;
  let turnsEl = $state(null);
  let consoleEl = $state(null);

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
  const eligibleWalkthroughTurns = $derived(walkthroughTurns(detail?.turns));
  // Runs and sessions are two collections, not one list with runs bolted on.
  // A session a run spawned is a detail of that run, reachable through it,
  // never a peer of a session you started yourself. Previously a run appeared
  // twice, once in an aggregate row and once as a group, and its children sat
  // at top level alongside standalone sessions.
  const isStandalone = (session) =>
    session?.workflow_id == null || session.workflow_id === "";
  const inbox = $derived(inboxGroups(runs, sessions, vms));
  const inboxEmpty = $derived(
    inbox.needsYou.length === 0 && inbox.running.length === 0,
  );
  const automaticRailMode = $derived(
    runsLoaded
      ? railState({
          needsYou: inbox.needsYou.length,
          running: inbox.running.length,
        })
      : "open",
  );
  const railMode = $derived(
    voiceFold
      ? "folded"
      : searchOpensRail && manualRail === null
        ? "open"
        : (manualRail ?? automaticRailMode),
  );
  const awakeGuests = $derived(
    Object.values(vms).filter((vm) => vm?.state === "awake").length,
  );
  const voiceSession = $derived(voiceSessionDetail?.session ?? null);
  const voiceVmState = $derived(
    voiceSession ? vmState(voiceSession, vms) : null,
  );
  const voiceTitle = $derived(
    voiceSession
      ? firstLine(voiceSession.title) || sessionTitle(voiceSession)
      : "",
  );
  // Recent is disjoint from the inbox: active sessions already have a row.
  const launcherSessions = $derived(
    fixture?.home
      ? (fixture.sessions ?? [])
      : sessions.filter((session) => !isActive(session)),
  );
  const launcherRuns = $derived(
    fixture?.home
      ? (fixture.master?.runs ?? []).filter((run) => run.completed_at)
      : terminalRuns,
  );
  const launcherRecent = $derived(
    recentSummary(
      launcherSessions,
      launcherRuns,
      fixture?.home && fixture.view?.now ? Date.parse(fixture.view.now) : now,
    ),
  );
  const launcherJumpTotal = $derived(jumpTotal(sessions, runs, terminalRuns));
  const visibleSearchResults = $derived(searchResults ?? []);
  const turnSearchHeading = $derived(
    `${P.labels.turnSearch}${P.punct.colon} ${P.labels.quoteMark}${searchQuery}${P.labels.quoteMark}`,
  );
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

  function formatRepoContext(session) {
    return session?.repo ? `${session.repo}@${session.branch || "main"}` : "";
  }

  function nearBottom() {
    if (!turnsEl) return false;
    return (
      turnsEl.scrollHeight - turnsEl.scrollTop - turnsEl.clientHeight < 200
    );
  }

  function autoScroll(force = false) {
    if (!turnsEl) return;
    if (force || nearBottom()) turnsEl.scrollTop = turnsEl.scrollHeight;
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
      availableModels =
        !Array.isArray(body) && body.localModelsOnly === true
          ? DEV_MODELS
          : MODELS;
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
    for (let attempt = 0; attempt < RUNS_LOAD_MAX_ATTEMPTS; attempt++) {
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
        const activeMaster = activeBody.master ?? activeBody;
        const activePartition = partitionRuns(activeMaster.runs ?? []);
        const terminalPartition = partitionRuns(
          (terminalBody.master ?? terminalBody).runs ?? [],
        );
        runs = activePartition.inFlight;
        terminalRuns = terminalPartition.terminal;
        runsLoaded = true;
        return;
      } catch {
        if (attempt < RUNS_LOAD_MAX_ATTEMPTS - 1) {
          await new Promise((resolve) =>
            setTimeout(resolve, RUNS_LOAD_BACKOFF_MS),
          );
        }
      }
    }
  }

  async function loadRunDetail(
    id,
    sequence = runRequestSequence,
    target = "console",
  ) {
    if (fixture) return;
    if (id == null) return;
    try {
      const response = await fetch(`/agents/runs/${encodeURIComponent(id)}`);
      if (!response.ok) throw new Error("swarm run unavailable");
      const body = await response.json();
      const normalized = {
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
      if (target === "voice") {
        voiceRunDetails = { ...voiceRunDetails, [String(id)]: normalized };
        return;
      }
      if (
        sequence === runRequestSequence &&
        String(selectedRunId) === String(id)
      ) {
        runDetail = normalized;
      }
    } catch {
      if (target === "voice") {
        voiceRunDetails = {
          ...voiceRunDetails,
          [String(id)]: {
            run: null,
            sessions: [],
            view: {
              engine_tier: "absent",
              now: new Date().toISOString(),
              snapshot_age_seconds: null,
            },
          },
        };
        return;
      }
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

  function openVoiceMode() {
    closeJump();
    navigateTo(setVoiceMode($page.url.searchParams, true));
  }

  function leaveVoiceMode() {
    navigateTo(setVoiceMode($page.url.searchParams, false));
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

  function updateVmStreamStatus(event) {
    vmStreamStatus = nextStatus($state.snapshot(vmStreamStatus), event);
    return vmStreamStatus;
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
      const body = await response.json().catch(() => null);
      if (!vmFallbackArmed) return;
      if (!response.ok) {
        updateVmStreamStatus({
          type: "poll-fail",
          error: body?.error ?? "Unable to load VM state",
        });
        return;
      }
      if (body == null) throw new Error("Invalid VM state response");
      updateVmStreamStatus({
        type: "poll-ok",
        at: Date.now(),
        error: body?.error ?? null,
      });
      // Apply every backend map regardless of indicator mode; see router.py:246-254.
      vms = body?.vms ?? {};
      vmSnapshotReceived = true;
    } catch (error) {
      // VM state is advisory; keep the last known map on transient failures.
      if (!vmFallbackArmed) return;
      updateVmStreamStatus({
        type: "poll-fail",
        error:
          error instanceof Error ? error.message : "Unable to load VM state",
      });
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

  // An inbox session is standalone: drop any selected run so the crumbs do
  // not claim it belongs to that run. RunView's callback keeps selectSession.
  function selectInboxSession(id) {
    if (id == null) return;
    navigateTo(sessionTransition(clearSelection($page.url.searchParams), id));
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
    needsInputState = false;
    pendingTaskId = null;
    tick().then(() => newButtonEl?.focus({ preventScroll: true }));
  }

  function openNewPanel() {
    needsInputState = false;
    pendingTaskId = null;
    showNewPanel = true;
  }

  function openJump(query = "") {
    jumpQuery = query;
    jumpOpen = true;
  }

  function closeJump() {
    jumpOpen = false;
  }

  function toggleJump() {
    if (jumpOpen) closeJump();
    else openJump();
  }

  function openNewSessionFromJump(text) {
    newSession.prompt = text;
    openNewPanel();
  }

  function clearTurnSearch() {
    searchController?.abort();
    searchController = null;
    searchQuery = "";
    searchResults = null;
    searchLoading = false;
    searchOpensRail = false;
  }

  function toggleSidebar() {
    if (voiceMode) return;
    manualRail = railMode === "folded" ? "open" : "folded";
    if (manualRail === "folded") clearTurnSearch();
  }

  async function runSearch(value = searchQuery) {
    const query = value.trim();
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
      if (!response.ok) throw new Error(P.labels.turnSearchUnavailable);
      const body = await response.json();
      if (!controller.signal.aborted) {
        searchResults = body.results ?? [];
        errorMessage = null;
      }
    } catch (error) {
      // Keep the previous result set for both aborted and failed requests.
      if (!controller.signal.aborted) errorMessage = error.message;
    } finally {
      if (searchController === controller) {
        searchController = null;
        searchLoading = false;
      }
    }
  }

  async function searchTurnsFromJump(text) {
    const query = text.trim();
    if (!query) {
      clearTurnSearch();
      return;
    }
    if (railMode === "folded") {
      if (manualRail === null) searchOpensRail = true;
      else toggleSidebar();
    }
    if (mobileTranscript) returnToSessionList();
    searchQuery = query;
    await runSearch(query);
  }

  function handleGlobalKeydown(event) {
    if (event.isComposing) return;
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      toggleJump();
    } else if (event.key === "Escape" && jumpOpen) {
      event.preventDefault();
      closeJump();
    } else if (event.key === "Escape" && showNewPanel) {
      event.preventDefault();
      closeNewPanel();
    }
  }

  async function sendSessionPrompt({ session_id, prompt: text, model }) {
    const requestBody = { prompt: String(text).trim() };
    if (model) requestBody.model = model;
    const response = await fetch(
      `/agents/session/${encodeURIComponent(session_id)}/messages`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(requestBody),
      },
    );
    const body = await response.json();
    if (!response.ok || body.accepted === false) {
      throw new Error(body.error || "Message was not accepted");
    }
    await loadSessions();
    if (String(selectedId) === String(session_id)) {
      await loadDetail(session_id, requestSequence, true);
    }
    if (String(voiceStage.attachedSessionId) === String(session_id)) {
      await loadVoiceSession(session_id);
    }
  }

  async function sendPrompt() {
    if (!selectedId || !prompt.trim() || sending) return;
    sending = true;
    errorMessage = null;
    try {
      await sendSessionPrompt({
        session_id: selectedId,
        prompt,
        model: composerModel,
      });
      prompt = "";
    } catch (error) {
      errorMessage = error.message;
    } finally {
      sending = false;
    }
  }

  async function sendVoicePrompt(message) {
    errorMessage = null;
    try {
      await sendSessionPrompt({ ...message, model: voiceSession?.model });
    } catch (error) {
      errorMessage = error.message;
      throw error;
    }
  }

  async function decideVoiceRun({ workflowId, nodeKey, decision, note = "" }) {
    errorMessage = null;
    try {
      const response = await fetch(
        `/agents/runs/${encodeURIComponent(workflowId)}/nodes/${encodeURIComponent(nodeKey)}/decision`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ decision, note }),
        },
      );
      if (!response.ok) {
        let detail = P.labels.decisionUnavailable;
        try {
          const body = await response.json();
          detail =
            typeof body?.detail === "string"
              ? body.detail
              : typeof body?.error === "string"
                ? body.error
                : detail;
        } catch {
          // Keep the stable fallback when the proxy response is not JSON.
        }
        throw new Error(detail);
      }
      return await response.json();
    } catch (error) {
      errorMessage = error.message;
      throw error;
    }
  }

  async function createTask() {
    if (!newSession.prompt.trim() || creating) return;
    creating = true;
    errorMessage = null;
    try {
      const requestBody = {
        task: newSession.prompt.trim(),
        ...(pendingTaskId ? { task_id: pendingTaskId } : {}),
        model: newSession.model || P.labels.defaultModel,
        repo: newSession.repo || null,
        branch: newSession.branch || null,
      };
      const response = await fetch("/api/swarm/classify-and-start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(requestBody),
      });
      const body = await response.json();
      if (!response.ok)
        throw new Error(body.detail || P.labels.taskCreateFailed);
      if (body.kind === "needs_input" && body.needs_input) {
        needsInputState = true;
        pendingTaskId = body.task_id;
        creating = false;
        if (!showNewPanel) showNewPanel = true;
        await tick();
        repoControlEl?.focus({ preventScroll: true });
        return;
      }
      if (showNewPanel) closeNewPanel();
      newSession = { prompt: "", model: "", repo: "", branch: "" };
      branches = [];
      needsInputState = false;
      pendingTaskId = null;
      if (body.kind === "run" && body.workflow_id) selectRun(body.workflow_id);
      else if (body.session_id) selectSession(body.session_id);
      // Released only after navigation, not before it. Clearing this on the
      // line after the fetch resolved re-enabled the button while the panel
      // was still on screen, which is a wide enough window on a phone to
      // land a second tap and start a second session.
      creating = false;
    } catch (error) {
      errorMessage = error.message;
      creating = false;
    }
  }
  async function destroySession() {
    if (!selectedId) return;
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
    if (showNewPanel && !needsInputState) {
      tick().then(() => newPromptEl?.focus());
    }
  });

  onMount(() => {
    loadRuns().then(() => {
      if (
        fixture ||
        $page.url.searchParams.has("run") ||
        $page.url.searchParams.has("session") ||
        $page.url.searchParams.get("mode") === "voice"
      ) {
        return;
      }
      // A phone lands on the inbox itself; desktop replaces (never pushes)
      // so the first Back press still leaves the console.
      if (isMobileViewport()) return;
      const groups = inboxGroups(runs, sessions, vms);
      const selection = arrivalSelection(groups.needsYou, groups.running);
      if (selection?.kind === "run")
        replaceWith(runSearchTransition($page.url.searchParams, selection.id));
      else if (selection?.kind === "session")
        replaceWith(sessionTransition($page.url.searchParams, selection.id));
    });
    const teardownViewport = setupVisualViewport(
      window,
      consoleEl,
      {
        measure: nearBottom,
        apply: (wasNearBottom) => {
          if (wasNearBottom) autoScroll(true);
        },
      },
      MOBILE_MEDIA_QUERY,
    );
    return () => {
      teardownViewport();
    };
  });

  $effect(() => {
    const sessionId = selectedId;
    const runId = selectedRunId;
    const wasSessionId = previousSessionId;
    previousSessionId = sessionId;

    if (String(sessionId) !== String(wasSessionId)) {
      sessionView = SESSION_VIEW_CONVERSATION;
      sessionViewInitializedFor = null;
    }

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
    const sessionId = selectedId;
    const turns = detail?.turns;
    if (
      sessionId == null ||
      turns == null ||
      !vmSnapshotReceived ||
      String(sessionViewInitializedFor) === String(sessionId)
    ) {
      return;
    }
    sessionView = defaultSessionView(vmState(selectedSession, vms), turns);
    sessionViewInitializedFor = sessionId;
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

  // The clock only refreshes the displayed age. It never changes connection
  // mode, which is driven exclusively by stream and fallback events.
  $effect(() => {
    if (vmStreamStatus.mode !== "polling" && vmStreamStatus.mode !== "stalled")
      return;
    vmStreamNow = Date.now();
    const interval = setInterval(() => {
      vmStreamNow = Date.now();
    }, 30_000);
    return () => clearInterval(interval);
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
      vmFallbackArmed = true;
      fallbackInterval = setInterval(loadVms, 2000);
      updateVmStreamStatus({ type: "fallback-armed" });
      loadVms();
    };

    const stopFallback = () => {
      vmFallbackArmed = false;
      clearInterval(fallbackInterval);
      fallbackInterval = undefined;
    };

    source.onopen = () => {
      failures = 0;
      stopFallback();
      updateVmStreamStatus({ type: "open" });
    };

    source.onmessage = (event) => {
      failures = 0;
      stopFallback();
      try {
        const body = JSON.parse(event.data);
        updateVmStreamStatus({
          type: "frame",
          at: Date.now(),
          error: body?.error ?? null,
        });
        // Apply every backend map regardless of indicator mode; see router.py:246-254.
        vms = body?.vms ?? {};
        vmSnapshotReceived = true;
      } catch (error) {
        // Retain the last snapshot and expose that the advisory frame failed.
        updateVmStreamStatus({
          type: "frame",
          at: Date.now(),
          error:
            error instanceof Error
              ? `Invalid VM stream payload: ${error.message}`
              : "Invalid VM stream payload",
        });
      }
    };
    source.onerror = () => {
      failures += 1;
      if (source.readyState !== EventSource.OPEN) {
        updateVmStreamStatus({ type: "closed" });
      }
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
      vmFallbackArmed = false;
      source.close();
      clearInterval(fallbackInterval);
    };
  });

  $effect(() => () => {
    searchController?.abort();
  });

  let previousInboxEmpty = null;
  $effect(() => {
    const empty = inboxEmpty;
    if (!runsLoaded) return;
    if (
      !voiceMode &&
      previousInboxEmpty !== null &&
      previousInboxEmpty !== empty
    ) {
      manualRail = null;
    }
    previousInboxEmpty = empty;
  });

  let previousAutomaticRailMode = null;
  $effect(() => {
    const automatic = automaticRailMode;
    if (automatic === "folded" && previousAutomaticRailMode !== "folded") {
      clearTurnSearch();
    }
    previousAutomaticRailMode = automatic;
  });

  function readVoiceStorage(key) {
    try {
      return window.localStorage.getItem(key);
    } catch {
      return null;
    }
  }

  function writeVoiceStorage(key, value) {
    try {
      if (value == null) window.localStorage.removeItem(key);
      else window.localStorage.setItem(key, String(value));
    } catch {
      // localStorage blocked; the companion remains available for this visit.
    }
  }

  async function registerVoiceCompanion(signal = AbortSignal.timeout(10000)) {
    const stored = readVoiceStorage(VOICE_STORAGE_ID);
    const response = await fetch("/agents/companion", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ companion_id: stored || null }),
      signal,
    });
    if (!response.ok) throw new Error("companion register failed");
    const body = await response.json();
    if (!body?.companion_id) throw new Error("companion register failed");
    voiceCompanionId = body.companion_id;
    writeVoiceStorage(VOICE_STORAGE_ID, voiceCompanionId);
    // The cursor is never persisted: a reload replays the whole ledger from
    // 0, which is the only way the stage, the binding and the wire come back
    // complete (the wire IS the ledger). cardPhase marks old cards gone.
    if (stored !== voiceCompanionId) voiceSince = 0;
  }

  async function loadVoiceSession(
    sessionId,
    signal = AbortSignal.timeout(10000),
    incremental = false,
  ) {
    if (sessionId == null) return;
    const sameSession =
      String(voiceSessionDetail?.session?.id) === String(sessionId);
    const useIncremental = incremental && sameSession;
    const maxSeq = useIncremental
      ? Math.max(
          0,
          ...(voiceSessionDetail?.turns ?? []).map((turn) => turn.seq),
        )
      : 0;
    const suffix = useIncremental ? `?after_seq=${maxSeq}` : "";
    const response = await fetch(
      `/agents/session/${encodeURIComponent(sessionId)}${suffix}`,
      { signal },
    );
    if (!response.ok) return;
    const body = await response.json();
    if (String(voiceStage.attachedSessionId) !== String(sessionId)) return;
    voiceSessionDetail = useIncremental
      ? {
          session: body.session,
          turns: [
            ...(voiceSessionDetail?.turns ?? []),
            ...(body.turns ?? []).filter(
              (turn) =>
                !(voiceSessionDetail?.turns ?? []).some(
                  (existing) => existing.seq === turn.seq,
                ),
            ),
          ],
          pending_queue: body.pending_queue,
        }
      : body;
  }

  function forgetVoiceCompanion() {
    voiceCompanionId = null;
    voiceSince = 0;
    voiceStage = emptyStage();
    voiceRows = [];
    voiceSessionDetail = null;
    voiceRunDetails = {};
    writeVoiceStorage(VOICE_STORAGE_ID, null);
  }

  async function pollVoiceLedger(signal = AbortSignal.timeout(10000)) {
    if (!voiceCompanionId) return;
    const response = await fetch(
      `/agents/companion/${encodeURIComponent(voiceCompanionId)}/ledger?since=${voiceSince}`,
      { signal },
    );
    const rows = response.ok ? await response.json() : [];
    const decision = decidePoll(
      { ok: response.ok, status: response.status, rows },
      voiceSince,
    );
    if (decision.forget) {
      forgetVoiceCompanion();
      return "unknown";
    }
    if (!response.ok) return;
    voiceNow = Date.now();
    if (decision.rows.length) {
      const known = new Set(voiceRows.map((row) => String(row.id)));
      voiceRows = [
        ...voiceRows,
        ...decision.rows.filter((row) => !known.has(String(row.id))),
      ].sort((a, b) => Number(a.id) - Number(b.id));
      voiceStage = applyLedgerRows(voiceStage, decision.rows);
      voiceSince = decision.cursor;
    }
    if (voiceStage.attachedSessionId != null) {
      await loadVoiceSession(voiceStage.attachedSessionId, signal, true);
    }
    for (const card of voiceStage.cards) {
      if (card.surface === "run" && !voiceRunDetails[card.ref]) {
        await loadRunDetail(card.ref, runRequestSequence, "voice");
      }
      const askRunId =
        card.kind === "ask" && String(card.ref).startsWith("run-")
          ? askWorkflowId(card)
          : null;
      if (askRunId && !voiceRunDetails[askRunId]) {
        await loadRunDetail(askRunId, runRequestSequence, "voice");
      }
    }
  }

  function pinVoiceCard(key) {
    voiceStage = togglePinned(voiceStage, key);
  }

  function dismissVoiceCard(key) {
    voiceStage = dismissCard(voiceStage, key);
  }

  function answerVoiceCard(key) {
    voiceStage = answerCard(voiceStage, key);
  }

  $effect(() => {
    const enabled = voiceMode;
    if (!enabled || typeof window === "undefined") return;
    let stopped = false;
    let inFlight = false;
    const controller = new AbortController();

    async function poll() {
      if (stopped || inFlight) return;
      inFlight = true;
      // A hung request must not wedge the loop: the leave signal AND a
      // timeout, else inFlight never clears and the stage silently freezes.
      const signal = AbortSignal.any([
        controller.signal,
        AbortSignal.timeout(10000),
      ]);
      try {
        if (!voiceCompanionId) {
          await registerVoiceCompanion(signal);
        }
        if (stopped) return;
        const result = await pollVoiceLedger(signal);
        if (result === "unknown" && !stopped) {
          await registerVoiceCompanion(signal);
          if (!stopped) await pollVoiceLedger(signal);
        }
      } catch {
        // Polling is its own heartbeat. A later tick retries without replacing
        // the last useful stage or surfacing a second console error channel.
      } finally {
        inFlight = false;
      }
    }

    function onVisibilityChange() {
      if (document.visibilityState === "visible") void poll();
    }

    // Run after dependency collection so cursor updates do not restart the
    // effect and turn the two-second heartbeat into a tight polling loop.
    queueMicrotask(() => void poll());
    const interval = setInterval(poll, VOICE_POLL_MS);
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      stopped = true;
      controller.abort();
      clearInterval(interval);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  });

  $effect(() => {
    const manual = manualRail;
    try {
      if (manual == null) {
        document.documentElement.removeAttribute("data-agents-rail");
        localStorage.removeItem("agents-sidebar-collapsed");
      } else {
        document.documentElement.setAttribute("data-agents-rail", manual);
        localStorage.setItem("agents-sidebar-collapsed", manual);
      }
    } catch (e) {
      // localStorage blocked; keep the in-memory preference
    }
  });
</script>

<svelte:head><title>{P.labels.pageTitle}</title></svelte:head>
<svelte:window onkeydown={handleGlobalKeydown} />

<main
  bind:this={consoleEl}
  class:mobile-transcript={mobileTranscript}
  class:rail-folded={railMode === "folded"}
  class:voice-mode={voiceMode}
  class="console"
>
  <header class="topbar">
    <div class="wordmark">Agents</div>
    {#if voiceMode}
      <span class="mode-pill">
        <span class="level-bars" aria-hidden="true"
          ><i></i><i></i><i></i><i></i></span
        >
        {P.labels.voice}
      </span>
      <span class="voice-attachment">
        {#if voiceStage.attachedSessionId != null}
          <span class="desktop-attachment">
            {P.labels.attachedTo}
            <span class="voice-session-id">#{voiceStage.attachedSessionId}</span
            >
            {voiceTitle}
          </span>
          <span class="phone-attachment">
            #{voiceStage.attachedSessionId}
            {P.punct.dot}
            {voiceSession?.model || P.labels.defaultModel}
            {P.punct.dot}
            {P.labels.vmWord}
            {voiceVmState || P.labels.unknownValue}
          </span>
          <span class="pill voice-vm-pill">
            {#if voiceVmState === "awake"}<span
                class="pill-dot"
                aria-hidden="true"
              ></span>{/if}
            {P.labels.vmWord}
            {voiceVmState || P.labels.unknownValue}
          </span>
        {:else}
          <span class="desktop-attachment">{P.labels.notAttached}</span>
          <span class="phone-attachment">{P.labels.notAttached}</span>
        {/if}
      </span>
    {/if}
    <button
      class="top-search"
      type="button"
      aria-label={P.labels.searchPlaceholder}
      aria-haspopup="dialog"
      onclick={() => openJump()}
    >
      <svg class="search-icon" viewBox="0 0 16 16" aria-hidden="true">
        <circle cx="7" cy="7" r="4.25"></circle>
        <path d="m10.25 10.25 3 3"></path>
      </svg>
      <span class="search-label">{P.labels.searchPlaceholder}</span>
      <kbd class="kbd">{P.labels.shortcutCommandK}</kbd>
    </button>
    <div class="guest-state">
      <span class:idle={awakeGuests === 0} class="awake-dot" aria-hidden="true"
      ></span>
      {awakeGuests}
      {P.labels.guestsAwake}
    </div>
    {#if voiceMode}
      <button class="leave-voice" type="button" onclick={leaveVoiceMode}
        >{P.labels.leaveVoice}</button
      >
    {:else}
      <button
        class="new-button"
        type="button"
        bind:this={newButtonEl}
        onclick={() => (showNewPanel ? closeNewPanel() : openNewPanel())}
      >
        <svg viewBox="0 0 16 16" aria-hidden="true">
          <path d="M8 3v10M3 8h10"></path>
        </svg>
        New
      </button>
    {/if}
  </header>

  <div class="shell">
    <aside class="inbox" aria-label={P.labels.sessionsRegion}>
      <div class="fold-rail">
        <button
          class="fold-button"
          type="button"
          aria-label={P.labels.expandInbox}
          aria-expanded="false"
          title={P.labels.expandInbox}
          onclick={toggleSidebar}
        >
          <svg viewBox="0 0 16 16" aria-hidden="true">
            <path d="m6 3 5 5-5 5"></path>
          </svg>
        </button>
        <span class="rail-hairline"></span>
        <button
          class:attention={inbox.needsYou.length > 0}
          class:idle={inboxEmpty}
          class="rail-badge"
          type="button"
          aria-label={`${inbox.needsYou.length} ${P.labels.needsYouExpandInbox}`}
          onclick={toggleSidebar}>{inbox.needsYou.length}</button
        >
        <button
          class:idle={inboxEmpty}
          class="rail-badge running"
          type="button"
          aria-label={`${inbox.running.length} ${P.labels.runningExpandInbox}`}
          onclick={toggleSidebar}
        >
          <span class="awake-dot" aria-hidden="true"></span>
          {inbox.running.length}
        </button>
      </div>

      <div class="inbox-expanded">
        <div class="inbox-head">
          <h1>{P.labels.inbox}</h1>
          <button
            class="fold-button"
            type="button"
            aria-label={P.labels.collapseInbox}
            aria-expanded="true"
            title={P.labels.collapseInbox}
            onclick={toggleSidebar}
          >
            <svg viewBox="0 0 16 16" aria-hidden="true">
              <path d="m10 3-5 5 5 5"></path>
            </svg>
          </button>
        </div>

        <div class="inbox-body">
          {#if (!fixture || fixture.home) && !selectedId && !selectedRunId && inboxEmpty && searchResults === null}
            <!-- The launcher stays in both responsive panes because CSS cannot
                 move one component across the detail and inbox scroll roots. -->
            <div class="mobile-home">
              <Launcher
                bind:session={newSession}
                models={availableModels}
                {repos}
                {branches}
                {repoLoading}
                {branchLoading}
                {creating}
                summary={launcherRecent}
                jumpCount={launcherJumpTotal}
                onLoadBranches={loadBranches}
                onSubmit={createTask}
                onOpenRun={selectRun}
                onOpenSession={selectInboxSession}
                onOpenJump={() => openJump()}
              />
            </div>
          {/if}
          {#if searchResults !== null}
            <div class="group">
              <div class="turn-search-head">
                <span class="turn-search-title">{turnSearchHeading}</span>
                {#if searchLoading}
                  <span class="turn-search-loading mono"
                    >{P.labels.searching}</span
                  >
                {/if}
                <button
                  type="button"
                  aria-label={P.labels.clearTurnSearch}
                  title={P.labels.clearTurnSearch}
                  onclick={clearTurnSearch}>{P.labels.clearMark}</button
                >
              </div>
              <div class="row-list">
                {#each visibleSearchResults as result (result.session_id + ":" + result.seq)}
                  <button
                    class:chosen={String(selectedId) ===
                      String(result.session_id)}
                    class="row search-result"
                    type="button"
                    onclick={() => selectSession(result.session_id)}
                  >
                    <span class="dot idle" aria-hidden="true"></span>
                    <span class="main">
                      <span class="row-title">{result.snippet}</span>
                      <span class="row-sub mono"
                        >{String(
                          result.local_session_id || result.workspace || "",
                        ).slice(0, 8)} · turn {result.seq}</span
                      >
                    </span>
                    <span class="age mono"
                      >{relativeTime(result.created_at)}</span
                    >
                  </button>
                {:else}<div class="empty">
                    {P.labels.noMatchingTurns}
                  </div>{/each}
              </div>
            </div>
          {:else}
            {#if inbox.needsYou.length}
              <div class="group attention-group">
                <div class="group-title">
                  <span>{P.labels.needsYou}</span>
                  <span class="group-count">{inbox.needsYou.length}</span>
                </div>
                <div class="row-list">
                  {#each inbox.needsYou as item (`${item.kind}:${item.id}`)}
                    {@render inboxRow(item, true)}
                  {/each}
                </div>
              </div>
            {/if}
            {#if inbox.running.length}
              <div class="group">
                <div class="group-title">
                  <span>{P.labels.runningGroup}</span>
                  <span class="group-count">{inbox.running.length}</span>
                </div>
                <div class="row-list">
                  {#each inbox.running as item (`${item.kind}:${item.id}`)}
                    {@render inboxRow(item, false)}
                  {/each}
                </div>
              </div>
            {/if}
            <button
              class="hist"
              type="button"
              aria-haspopup="dialog"
              onclick={() => openJump()}
            >
              <span
                >{P.labels.sessionsAndRunsInJump.replace(
                  "{count}",
                  String(launcherJumpTotal),
                )}</span
              >
              <kbd class="kbd">{P.labels.shortcutCommandK}</kbd>
            </button>
          {/if}
        </div>

        <div
          class={`inbox-foot mono ${vmStreamStatus.mode}`}
          title={vmStreamStatus.error ?? P.labels.vmStreamState}
        >
          <span class="vm-stream-dot" aria-hidden="true"></span>
          {#if vmStreamStatus.error}<span class="sr-only"
              >: {vmStreamStatus.error}</span
            >{/if}
          {#if vmStreamStatus.mode === "streaming"}
            {P.labels.vmStreamLive} · {awakeGuests} {P.labels.guestsAwake}
          {:else if vmStreamStatus.mode === "polling"}
            {P.labels.vmStreamPolling}{#if vmStreamStatus.lastUpdateAt != null}
              · {P.labels.updated}
              {streamAge(vmStreamNow - vmStreamStatus.lastUpdateAt)}{/if}
          {:else if vmStreamStatus.mode === "stalled"}
            {P.labels.vmStreamStalled}{#if vmStreamStatus.lastUpdateAt != null}
              · {P.labels.updated}
              {streamAge(vmStreamNow - vmStreamStatus.lastUpdateAt)}{/if}
          {:else}
            {P.labels.vmStreamConnecting}
          {/if}
        </div>
      </div>
    </aside>

    <section
      class="detail transcript"
      aria-label={voiceMode
        ? P.labels.voiceCompanion
        : P.labels.transcriptRegion}
    >
      {#if voiceMode}
        <VoiceCompanion
          stage={voiceStage}
          rows={voiceRows}
          sessionDetail={voiceSessionDetail}
          {vms}
          runDetails={voiceRunDetails}
          now={voiceNow}
          {renderedPending}
          onPin={pinVoiceCard}
          onDismiss={dismissVoiceCard}
          onSend={sendVoicePrompt}
          onDecide={decideVoiceRun}
          onAnswered={answerVoiceCard}
        />
      {:else}
        {#if !(selectedId && selectedSession)}
          <div class="mobile-detail-nav">
            <button
              class="mobile-back"
              type="button"
              aria-label={P.labels.backToSessions}
              onclick={returnToSessionList}
            >
              <svg viewBox="0 0 18 18" aria-hidden="true">
                <path d="m11 4-5 5 5 5"></path>
              </svg>
            </button>
            <button
              class="mobile-jump"
              type="button"
              aria-label={P.labels.jumpOpenLabel}
              aria-haspopup="dialog"
              onclick={() => openJump()}
            >
              <svg viewBox="0 0 16 16" aria-hidden="true">
                <circle cx="7" cy="7" r="4.25"></circle>
                <path d="m10.25 10.25 3 3"></path>
              </svg>
            </button>
          </div>
        {/if}
        {#if fixture?.walkthrough}
          <!-- Walkthrough visual states are reviewed via ?fixture=walk-*; the
           component gets its payload inline and never fetches. -->
          <div class="walkthrough-page">
            <div class="walkthrough-inner">
              <h2>{P.labels.walkSummary}</h2>
              <WalkthroughNarrative
                turnSeq={fixture.walkthrough.turnSeq}
                fixture={fixture.walkthrough}
              />
            </div>
          </div>
        {:else if fixture && !fixture.home}
          <RunView
            run={fixture.run}
            view={fixture.view}
            sessions={fixture.sessions}
            onSelectSession={selectSession}
            onCrumb={paneCrumb}
            onVoice={openVoiceMode}
            onError={(message) => (errorMessage = message)}
          />
        {:else if selectedId && selectedSession}
          <header class="transcript-head">
            <button
              class="mobile-back"
              type="button"
              aria-label={P.labels.backToSessions}
              onclick={returnToSessionList}
            >
              <svg viewBox="0 0 18 18" aria-hidden="true">
                <path d="m11 4-5 5 5 5"></path>
              </svg>
            </button>
            <PaneHeader
              sessionRow
              crumbs={sessionCrumbs}
              onCrumb={paneCrumb}
              selectedRun={Boolean(selectedRunId)}
              sessionId={selectedSession.local_session_id}
              {sessionView}
              onBackToRun={returnToRun}
              onChangeView={(view) => (sessionView = view)}
              onDestroy={destroySession}
              onVoice={openVoiceMode}
            >
              <h1
                class="session-title"
                title={headerTitle}
                tabindex="-1"
                bind:this={titleEl}
              >
                <span class="session-title-text">{headerTitle}</span>
                <span class="session-mobile-meta mono">
                  {statusLabel(selectedSession)}
                  {P.punct.dot}
                  {selectedSession.model ||
                    "luna"}{#if formatRepoContext(selectedSession)}
                    {P.punct.dot} {formatRepoContext(selectedSession)}
                  {/if}
                </span>
              </h1>
              <span
                class="pill"
                title={vms[selectedSession.ember_session_id]?.cp_state
                  ? P.labels.controlPlaneState.replace(
                      "{state}",
                      vms[selectedSession.ember_session_id].cp_state,
                    )
                  : P.labels.noLiveVm}
              >
                {#if vmState(selectedSession, vms) === "awake"}
                  <span class="pill-dot" aria-hidden="true"></span>
                {/if}
                {P.labels.vmWord}
                {vmState(selectedSession, vms)}
              </span>
              <span class="pill">{selectedSession.model || "luna"}</span>
              {#if formatRepoContext(selectedSession)}
                <span class="pill">{formatRepoContext(selectedSession)}</span>
              {/if}
              {#if ["needs_input", "warn"].includes(statusClass(selectedSession))}
                <span class={`pill state-pill ${statusClass(selectedSession)}`}
                  >{statusLabel(selectedSession)}</span
                >
              {/if}
              <span
                class="seg"
                role="group"
                aria-label={P.labels.sessionViewLabel}
              >
                <button
                  type="button"
                  class:selected={sessionView === SESSION_VIEW_CONVERSATION}
                  aria-pressed={sessionView === SESSION_VIEW_CONVERSATION}
                  onclick={() => (sessionView = SESSION_VIEW_CONVERSATION)}
                  >{P.labels.conversationView}</button
                >
                <button
                  type="button"
                  class:selected={sessionView === SESSION_VIEW_WALKTHROUGH}
                  aria-pressed={sessionView === SESSION_VIEW_WALKTHROUGH}
                  onclick={() => (sessionView = SESSION_VIEW_WALKTHROUGH)}
                  >{P.labels.walkthroughView}</button
                >
              </span>
            </PaneHeader>
            <button
              class="mobile-jump"
              type="button"
              aria-label={P.labels.jumpOpenLabel}
              aria-haspopup="dialog"
              onclick={() => openJump()}
            >
              <svg viewBox="0 0 16 16" aria-hidden="true">
                <circle cx="7" cy="7" r="4.25"></circle>
                <path d="m10.25 10.25 3 3"></path>
              </svg>
            </button>
          </header>
          {#if sessionView === SESSION_VIEW_CONVERSATION}
            <Turns
              {detail}
              {selectedSession}
              {renderedPending}
              {vms}
              bind:element={turnsEl}
            />
          {:else}
            <div class="walkthrough-page">
              <div class="walkthrough-inner">
                <h2>{P.labels.walkSummary}</h2>
                {#each eligibleWalkthroughTurns as turn (turn.seq)}
                  <WalkthroughNarrative
                    sessionId={selectedSession.id}
                    turnSeq={turn.seq}
                    walkthroughTurnCount={eligibleWalkthroughTurns.length}
                  />
                {:else}
                  <div class="empty walkthrough-empty">
                    {P.labels.walkthroughUnavailableForSession}
                  </div>
                {/each}
              </div>
            </div>
          {/if}

          <form
            class="composer"
            onsubmit={(event) => {
              event.preventDefault();
              sendPrompt();
            }}
          >
            <div class="box">
              <textarea
                bind:value={prompt}
                placeholder={P.labels.replyTo.replace(
                  "{model}",
                  composerModel || selectedSession.model || "luna",
                )}
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
              <div class="bar">
                {@render modelPicker(
                  composerModel,
                  (model) => {
                    composerModelOverride = model;
                  },
                  true,
                )}
                <span class="composer-hint mono">{P.labels.sendHint}</span>
                <button
                  class="composer-submit"
                  type="submit"
                  aria-label={sending ? P.labels.sending : P.labels.sendPrompt}
                  disabled={sending || !prompt.trim()}
                >
                  <svg viewBox="0 0 18 18" aria-hidden="true">
                    <path d="M9 14V4m0 0L5 8m4-4 4 4"></path>
                  </svg>
                </button>
              </div>
            </div>
          </form>
        {:else if selectedId}
          <!-- ?session= for a row absent from the server-rendered list. Without
           this branch the pane falls through to the launcher and swaps
           once loadDetail resolves. -->
          <div class="loading-session">
            <PaneHeader kind={P.labels.session} />
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
              onVoice={openVoiceMode}
              onError={(message) => (errorMessage = message)}
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
          <Launcher
            bind:session={newSession}
            models={availableModels}
            {repos}
            {branches}
            {repoLoading}
            {branchLoading}
            {creating}
            summary={launcherRecent}
            jumpCount={launcherJumpTotal}
            onLoadBranches={loadBranches}
            onSubmit={createTask}
            onOpenRun={selectRun}
            onOpenSession={selectInboxSession}
            onOpenJump={() => openJump()}
          />
        {/if}
      {/if}
    </section>
  </div>

  {#if showNewPanel}
    <button
      class="new-panel-scrim"
      type="button"
      aria-label="Close new session panel"
      onclick={closeNewPanel}
    ></button>
    <div class="new-panel" role="dialog" aria-label={P.labels.submitTask}>
      <div class="eyebrow">{P.labels.submitTask}</div>
      <form
        onsubmit={(event) => {
          event.preventDefault();
          createTask();
        }}
      >
        <label
          >{P.labels.task}<textarea
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
                createTask();
              }
            }}></textarea></label
        >
        <div class="field">
          <span class="field-label">{P.labels.modelWord}</span>
          {@render modelPicker(newSession.model, (model) => {
            newSession.model = model;
          })}
        </div>
        <label
          >{P.labels.repoWord}<select
            bind:this={repoControlEl}
            class:needs-input={needsInputState}
            aria-invalid={needsInputState}
            class="mono"
            bind:value={newSession.repo}
            disabled={repoLoading}
            onchange={() => {
              newSession.branch = "";
              loadBranches(newSession.repo);
            }}
          >
            {#if repoLoading}
              <option value="">{P.labels.loadingRepos}</option>
            {:else}
              <option value="">{P.labels.scratchWorkspace}</option>
              {#each repos as repo}
                <option value={repo.id} title={repo.description || ""}>
                  {repo.id}
                </option>
              {/each}
            {/if}
          </select></label
        >
        {#if needsInputState}
          <div class="needs-input-message" role="status">
            {P.labels.plannedNeedsRepoBranch}
          </div>
        {/if}
        <label>
          {P.labels.branchWord}<select
            class="mono"
            bind:value={newSession.branch}
            disabled={!newSession.repo || branchLoading}
          >
            {#if branchLoading}
              <option value="">{P.labels.loadingBranches}</option>
            {:else if branches.length === 0}
              <option value={P.labels.defaultBranch}
                >{P.labels.defaultBranch}</option
              >
            {:else}
              {#each branches as branch}
                <option value={branch.name}>{branch.name}</option>
              {/each}
            {/if}
          </select>
        </label>
        <div class="new-actions">
          <button type="button" class="quiet-button" onclick={closeNewPanel}
            >{P.labels.cancelWord}</button
          ><button
            class="primary-button"
            type="submit"
            disabled={creating || !newSession.prompt.trim()}
            >{creating ? P.labels.creating : P.labels.submitTask}</button
          >
        </div>
      </form>
    </div>
  {/if}

  <JumpPalette
    open={jumpOpen}
    bind:query={jumpQuery}
    {sessions}
    {runs}
    {terminalRuns}
    {inbox}
    onClose={closeJump}
    onOpenRun={selectRun}
    onOpenSession={selectInboxSession}
    onNewSession={openNewSessionFromJump}
    onSearchTurns={searchTurnsFromJump}
    onOpenVoice={openVoiceMode}
  />

  {#if errorMessage}<div class="error-banner" role="status">
      {errorMessage}
    </div>{/if}
</main>

{#snippet inboxRow(item, attention)}
  {@const entry = item.value}
  <button
    class:chosen={item.kind === "run"
      ? String(selectedRunId) === String(item.id)
      : String(selectedId) === String(item.id)}
    class:attn={attention}
    class="row"
    id={item.kind === "session"
      ? `agent-session-${String(item.id)}`
      : undefined}
    type="button"
    aria-label={item.kind === "run"
      ? `${firstLine(entry.title || entry.task?.text)}: ${P.stateWords[entry.state] || entry.state}`
      : `${sessionTitle(entry)}: ${statusLabel(entry)}`}
    onclick={() =>
      item.kind === "run" ? selectRun(item.id) : selectInboxSession(item.id)}
  >
    {#if item.kind === "run"}
      <span class="run-shape-strip" aria-hidden="true">
        {#each entry.shape?.length ? entry.shape : [{ key: "run", kind: "work", state: entry.state }] as node, index (`${node.key}:${index}`)}
          <span
            class:gate={node.kind === "gate"}
            class={`shape-node ${shapeStateClass(entry, node)}`}
          ></span>
        {/each}
      </span>
    {:else}
      <span class={`dot ${statusClass(entry)}`} title={statusLabel(entry)}
      ></span>
    {/if}
    <span class="main">
      <span class="row-title">
        {item.kind === "run"
          ? firstLine(entry.title || entry.task?.text) || entry.workflow_id
          : sessionTitle(entry)}
      </span>
      <span class="row-sub mono">
        {#if item.kind === "run"}
          run · {P.stateWords[entry.state] || entry.state} · {fmtCost(
            entry.cost_usd,
          ) || "$0.00"}
        {:else}
          {entry.model || "luna"} · {entry.repo
            ? `${entry.repo}@${entry.branch || "main"}`
            : "no repo"}
        {/if}
      </span>
    </span>
    {#if attention}
      <span class="ask">
        {item.kind === "run" ? runAsk(entry) : P.labels.answer}
      </span>
    {/if}
    <span class="age mono">{relativeTime(item.activityAt)}</span>
  </button>
{/snippet}

{#snippet modelPicker(current, choose, composer = false)}
  <label class:composer-model={composer} class="model-picker">
    <span class="sr-only">{P.labels.modelPicker}</span>
    <select
      class="mono"
      aria-label={P.labels.modelPicker}
      value={current}
      onchange={(event) => choose(event.currentTarget.value)}
    >
      <option value="">{P.labels.defaultWord}</option>
      {#if current && !availableModels.includes(current)}
        <option value={current}>{current}</option>
      {/if}
      {#each availableModels as model}<option value={model}>{model}</option
        >{/each}
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
    height: var(--console-h, 100dvh);
    display: flex;
    flex-direction: column;
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
  .console select.mono {
    font-family: var(--font-mono);
  }
  .mono {
    font-size: var(--size-body-mono);
  }
  button,
  textarea,
  select {
    font: inherit;
  }
  button {
    cursor: pointer;
  }
  button,
  textarea,
  select {
    border-radius: var(--radius-md);
  }
  button:focus-visible,
  textarea:focus-visible,
  select:focus-visible {
    outline: 2px solid var(--info);
    outline-offset: 2px;
  }
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
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }
  .new-button,
  .quiet-button,
  .primary-button {
    height: 32px;
    padding: 0 12px;
    border: 1px solid var(--line-strong);
    border-radius: var(--radius-md);
    font-size: 13px;
    line-height: 1;
  }
  .new-button,
  .quiet-button {
    color: var(--text);
    background: var(--panel-bg);
  }
  .new-button:hover,
  .quiet-button:hover {
    background: var(--hover);
  }
  .new-panel textarea,
  .composer textarea,
  select {
    width: 100%;
    color: var(--text);
    background: var(--panel-bg);
    border: 1px solid var(--line-strong);
    padding: 0 8px;
    outline: none;
  }
  select {
    height: 30px;
  }
  .new-panel textarea,
  .composer textarea {
    padding: 8px 8px;
  }
  textarea:focus,
  select:focus {
    border-color: var(--info);
  }
  select {
    appearance: none;
    padding-right: 27px;
  }
  .primary-button {
    color: var(--ink-text);
    background: var(--ink);
    border-color: var(--ink);
    font-weight: 600;
  }
  .primary-button:disabled {
    cursor: not-allowed;
    opacity: 0.45;
  }
  .topbar {
    flex: 0 0 52px;
    height: 52px;
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 0 12px 0 20px;
    border-bottom: 1px solid var(--line);
    background: var(--panel-bg);
  }
  .wordmark {
    flex: 0 0 auto;
    font-size: 14px;
    font-weight: 600;
  }
  .mode-pill {
    height: 32px;
    display: inline-flex;
    flex: 0 0 auto;
    align-items: center;
    gap: 8px;
    padding: 0 12px;
    border-radius: var(--radius-pill);
    color: var(--info);
    background: var(--info-soft);
    font-size: 13px;
    font-weight: 500;
  }
  .level-bars {
    height: 14px;
    display: inline-flex;
    align-items: flex-end;
    gap: 2px;
  }
  .level-bars i {
    width: 3px;
    border-radius: 1px;
    background: var(--info);
    transform-origin: bottom;
  }
  .level-bars i:nth-child(1) {
    height: 6px;
  }
  .level-bars i:nth-child(2) {
    height: 14px;
  }
  .level-bars i:nth-child(3) {
    height: 9px;
  }
  .level-bars i:nth-child(4) {
    height: 12px;
  }
  .voice-attachment {
    min-width: 0;
    display: flex;
    align-items: center;
    gap: 8px;
    color: var(--text-soft);
    font-size: 13px;
  }
  .desktop-attachment {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .phone-attachment {
    display: none;
  }
  .voice-session-id {
    color: var(--muted);
    font-family: var(--font-mono);
  }
  .voice-vm-pill {
    flex: 0 0 auto;
  }
  .leave-voice {
    height: 32px;
    padding: 0 12px;
    border: 1px solid var(--line-strong);
    border-radius: var(--radius-md);
    color: var(--text);
    background: var(--panel-bg);
    font-size: 13px;
  }
  .leave-voice:hover {
    background: var(--hover);
  }
  .top-search {
    flex: 0 1 320px;
    width: 320px;
    height: 32px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    min-width: 0;
    padding: 0 8px 0 10px;
    border: 1px solid var(--line);
    border-radius: var(--radius-md);
    color: var(--muted);
    background: var(--panel-bg);
    font-size: 13px;
    text-align: left;
  }
  .search-icon {
    display: none;
  }
  .top-search:hover {
    background: var(--hover);
  }
  .top-search:focus-visible {
    border-color: var(--info);
  }
  .top-search span {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  kbd {
    border: 0;
    color: var(--muted);
    background: transparent;
    font: 11.5px var(--font-mono);
    white-space: nowrap;
  }
  .top-search kbd {
    margin-left: 8px;
  }
  .guest-state {
    margin-left: auto;
    display: flex;
    align-items: center;
    gap: 6px;
    color: var(--muted);
    font-size: 12px;
    white-space: nowrap;
  }
  .awake-dot {
    width: 6px;
    height: 6px;
    flex: 0 0 6px;
    border-radius: var(--radius-circle);
    background: var(--ok);
  }
  .awake-dot.idle {
    background: var(--dot-idle);
  }
  .new-button {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    color: var(--ink-text);
    background: var(--ink);
    border-color: var(--ink);
    font-weight: 500;
  }
  .new-button svg {
    width: 14px;
    height: 14px;
    fill: none;
    stroke: currentColor;
    stroke-width: 1.5;
    stroke-linecap: round;
  }
  .new-button:hover {
    color: var(--ink-text);
    background: var(--ink);
  }
  .shell {
    flex: 1;
    min-height: 0;
    display: flex;
  }
  .inbox {
    flex: 0 0 440px;
    min-width: 0;
    min-height: 0;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    border-right: 1px solid var(--line);
    background: var(--page-bg);
  }
  .inbox-expanded {
    min-height: 0;
    height: 100%;
    display: flex;
    flex-direction: column;
  }
  .inbox-head {
    flex: 0 0 auto;
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 8px 0 20px;
  }
  .inbox-head h1 {
    margin: 0;
    font-size: 13px;
    font-weight: 600;
  }
  .fold-button {
    width: 36px;
    height: 36px;
    flex: 0 0 36px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 0;
    border: 0;
    border-radius: var(--radius-md);
    color: var(--muted);
    background: transparent;
  }
  .fold-button:hover {
    color: var(--text);
    background: var(--hover);
  }
  .fold-button svg {
    width: 16px;
    height: 16px;
    fill: none;
    stroke: currentColor;
    stroke-width: 1.5;
    stroke-linecap: round;
    stroke-linejoin: round;
  }
  .fold-rail {
    display: none;
  }
  .inbox-body {
    flex: 1;
    min-height: 0;
    overflow: auto;
    padding: 4px 8px 12px;
  }
  .mobile-home {
    display: none;
  }
  .group {
    margin-top: 12px;
  }
  .group-title {
    display: flex;
    justify-content: space-between;
    margin: 0;
    padding: 0 12px 6px;
    color: var(--muted);
    font-size: 11.5px;
    font-weight: 600;
    line-height: 1.2;
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }
  .group-count {
    font-family: var(--font-mono);
    font-weight: 600;
  }
  .turn-search-head {
    min-height: 36px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 0 4px 0 12px;
    color: var(--muted);
    font-size: var(--size-meta);
  }
  .turn-search-title {
    min-width: 0;
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .turn-search-loading {
    flex: 0 0 auto;
    color: var(--muted);
    font-size: 11.5px;
  }
  .turn-search-head button {
    width: 36px;
    height: 36px;
    flex: 0 0 36px;
    padding: 0;
    border: 0;
    color: var(--muted);
    background: transparent;
    font-size: 18px;
  }
  .turn-search-head button:hover {
    color: var(--text);
    background: var(--hover);
  }
  .attention-group .group-title {
    color: var(--attn-text);
  }
  .row-list {
    display: grid;
    gap: 2px;
  }
  .row,
  .row.search-result {
    width: 100%;
    min-height: 52px;
    min-width: 0;
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 8px 12px;
    border: 1px solid transparent;
    border-radius: 6px;
    color: inherit;
    background: transparent;
    text-align: left;
  }
  .row:hover,
  .row.search-result:hover {
    background: var(--hover);
  }
  .row.chosen {
    border-color: var(--line);
    background: var(--panel-bg);
  }
  .row.attn {
    border-color: var(--attn-soft);
    background: var(--attn-soft);
  }
  .row.attn.chosen {
    border-color: var(--line-strong);
    background: var(--attn-soft);
  }
  .row .dot {
    width: 8px;
    height: 8px;
    flex: 0 0 8px;
    margin: 0;
    border-radius: var(--radius-circle);
    background: var(--dot-idle);
  }
  .dot.idle,
  .dot.completed {
    background: var(--dot-idle);
  }
  .dot.running,
  .dot.working {
    background: var(--ok);
  }
  .dot.needs_input {
    background: var(--attn);
  }
  .dot.warn {
    background: var(--err);
  }
  .run-shape-strip {
    flex: 0 0 auto;
    display: inline-flex;
    align-items: center;
    gap: 3px;
  }
  .shape-node {
    width: 7px;
    height: 7px;
    flex: 0 0 7px;
    border-radius: 2px;
    background: currentColor;
  }
  .shape-node.gate {
    transform: rotate(45deg) scale(0.85);
  }
  .main {
    min-width: 0;
    flex: 1;
    display: grid;
    gap: 3px;
  }
  .row-title,
  .row-sub {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .row-title {
    color: var(--text);
    font-size: 14px;
    font-weight: 500;
  }
  .row-sub {
    color: var(--muted);
    font-size: 12px;
  }
  .ask {
    flex: 0 0 auto;
    color: var(--attn-text);
    font-size: 13px;
    font-weight: 600;
    white-space: nowrap;
  }
  .age {
    flex: 0 0 44px;
    width: 44px;
    color: var(--muted);
    font-size: 12px;
    text-align: right;
    white-space: nowrap;
  }
  .hist {
    width: 100%;
    height: 36px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-top: 10px;
    padding: 0 12px;
    border: 0;
    border-radius: var(--radius-md);
    color: var(--muted);
    background: transparent;
    font-size: 12px;
    text-align: left;
  }
  .hist:hover {
    color: var(--text);
    background: var(--hover);
  }
  .inbox-foot {
    flex: 0 0 auto;
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 10px 16px 14px;
    border-top: 1px solid var(--line);
    color: var(--muted);
    font-size: 11.5px;
  }
  .inbox-foot.streaming .vm-stream-dot,
  .inbox-foot.polling .vm-stream-dot {
    background: var(--muted);
  }
  .inbox-foot.stalled {
    color: var(--attn-text);
  }
  .inbox-foot.stalled .vm-stream-dot {
    background: var(--attn-text);
  }
  .rail-hairline {
    width: 24px;
    height: 1px;
    background: var(--line);
  }
  .rail-badge {
    width: 40px;
    height: 40px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 5px;
    padding: 0;
    border: 0;
    border-radius: 6px;
    color: var(--text-soft);
    background: transparent;
    font: 600 13px var(--font-mono);
  }
  .rail-badge:hover {
    background: var(--hover);
  }
  .rail-badge.attention {
    color: var(--attn-text);
    background: var(--attn-soft);
  }
  .rail-badge.idle {
    color: var(--dot-idle);
  }
  .rail-badge.idle .awake-dot {
    background: var(--line);
  }
  .model-picker {
    display: block;
    flex: 1;
  }
  .model-picker select {
    height: 28px;
    border-radius: var(--radius-md);
    color: var(--text);
    background: transparent;
    font-size: 12.5px;
  }
  .model-picker select:hover {
    background: var(--hover);
  }
  .transcript {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    min-height: 0;
    overflow-x: hidden;
    background: var(--panel-bg);
  }
  .transcript-head {
    flex: 0 0 52px;
    height: 52px;
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 0 20px 0 24px;
    border-bottom: 1px solid var(--line);
  }
  .session-title {
    min-width: 0;
    margin: 0;
    overflow: hidden;
    font-size: 15px;
    font-weight: 600;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .session-title-text {
    display: block;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .session-mobile-meta {
    display: none;
  }
  .session-title:focus:not(:focus-visible) {
    outline: none;
  }
  .state-pill.needs_input {
    color: var(--attn-text);
  }
  .state-pill.warn {
    color: var(--err);
  }
  .vm-stream-dot {
    width: 6px;
    height: 6px;
    border-radius: var(--radius-circle);
    background: var(--muted);
  }
  .walkthrough-page {
    flex: 1;
    min-width: 0;
    padding: 20px 28px;
    overflow-x: hidden;
    overflow-y: auto;
  }
  .walkthrough-inner {
    width: 100%;
    max-width: 1040px;
    min-width: 0;
    margin: 0 auto;
  }
  .walkthrough-inner > h2 {
    margin: 0;
    color: var(--text);
    font: 700 var(--size-body) var(--font-mono);
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }
  .walkthrough-empty {
    margin-top: 24px;
  }
  .composer {
    border-top: 1px solid var(--line);
    padding: 12px 28px 16px;
  }
  .box {
    max-width: 720px;
    margin: 0 auto;
    /* No overflow clip: it would cut the focus rings of the controls inside. */
    border: 1px solid var(--line-strong);
    border-radius: 6px;
    background: var(--panel-bg);
  }
  .box:focus-within {
    border-color: var(--info);
    outline: 2px solid var(--info);
    outline-offset: 1px;
  }
  .composer .box textarea {
    min-height: 64px;
    resize: none;
    padding: 12px 14px 6px;
    border: 0;
    border-radius: 0;
    background: transparent;
    font-size: 14px;
    line-height: 1.5;
    outline: none;
  }
  .bar {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 4px 8px 8px 10px;
  }
  .model-picker.composer-model {
    position: relative;
    flex: 0 0 auto;
  }
  .model-picker.composer-model::after {
    position: absolute;
    top: 50%;
    right: 7px;
    color: var(--muted);
    content: "▾";
    font: 12px var(--font-mono);
    pointer-events: none;
    transform: translateY(-55%);
  }
  .model-picker.composer-model select {
    width: auto;
    min-width: 62px;
    height: 28px;
    padding: 0 24px 0 7px;
    border: 0;
    color: var(--text-soft);
    background: transparent;
    font: 12px var(--font-mono);
  }
  .composer-hint {
    margin-left: auto;
    color: var(--muted);
    font-size: 11.5px;
    white-space: nowrap;
  }
  .composer-submit {
    width: 32px;
    height: 32px;
    display: inline-flex;
    flex: 0 0 32px;
    align-items: center;
    justify-content: center;
    padding: 0;
    border: 1px solid var(--ink);
    border-radius: 4px;
    color: var(--ink-text);
    background: var(--ink);
  }
  .composer-submit svg {
    width: 18px;
    height: 18px;
    fill: none;
    stroke: currentColor;
    stroke-width: 1.5;
    stroke-linecap: round;
    stroke-linejoin: round;
  }
  .composer-submit:disabled {
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
    border-left: 1px solid var(--line-strong);
    padding: 20px;
    box-shadow: var(--panel-shadow);
  }
  .new-panel-scrim,
  .mobile-detail-nav,
  .mobile-back,
  .mobile-jump {
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
    letter-spacing: 0.04em;
  }
  .new-panel textarea {
    resize: vertical;
    color: var(--text);
    text-transform: none;
    letter-spacing: normal;
    font-size: var(--size-body);
  }
  .new-panel select.needs-input {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }
  .needs-input-message {
    color: var(--accent);
    font-size: var(--size-meta);
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
    border: 1px solid var(--err-line);
    border-radius: var(--radius-lg);
    color: var(--err);
    background: var(--err-bg);
    font-size: var(--size-detail);
  }
  @media (prefers-reduced-motion: no-preference) {
    .dot.working {
      animation: pulse 1.2s ease-in-out infinite;
    }
    .level-bars i:nth-child(odd) {
      animation: voice-level 900ms ease-in-out infinite alternate;
    }
    .level-bars i:nth-child(even) {
      animation: voice-level 700ms ease-in-out infinite alternate-reverse;
    }
  }
  /* The static shell applies a manual fold before hydration. The component
     class also covers the automatic empty-inbox state. */
  :global(html[data-agents-rail="folded"]) .console .inbox,
  .console.rail-folded .inbox {
    flex-basis: 56px;
  }
  :global(html[data-agents-rail="folded"]) .console .inbox-expanded,
  .console.rail-folded .inbox-expanded {
    display: none;
  }
  :global(html[data-agents-rail="folded"]) .console .fold-rail,
  .console.rail-folded .fold-rail {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 6px;
    padding-top: 8px;
  }
  /* A persisted manual "open" must beat the server-rendered automatic fold
     before hydration, or a quiet day flashes a rail the user unfolded. */
  :global(html[data-agents-rail="open"]) .console.rail-folded .inbox {
    flex-basis: 440px;
  }
  :global(html[data-agents-rail="open"]) .console.rail-folded .inbox-expanded {
    display: flex;
  }
  :global(html[data-agents-rail="open"]) .console.rail-folded .fold-rail {
    display: none;
  }
  :global(html[data-agents-rail="open"]) .console.voice-mode .inbox,
  .console.voice-mode.rail-folded .inbox {
    flex-basis: 56px;
  }
  :global(html[data-agents-rail="open"]) .console.voice-mode .inbox-expanded,
  .console.voice-mode.rail-folded .inbox-expanded {
    display: none;
  }
  :global(html[data-agents-rail="open"]) .console.voice-mode .fold-rail,
  .console.voice-mode.rail-folded .fold-rail {
    display: flex;
    flex-direction: column;
    align-items: center;
  }
  @keyframes pulse {
    50% {
      opacity: 0.35;
    }
  }
  @keyframes voice-level {
    50% {
      transform: scaleY(0.45);
    }
  }
  /* Matches MOBILE_MEDIA_QUERY at the top of this file */
  @media (max-width: 760px) {
    .topbar {
      gap: 8px;
      padding: 0 8px;
    }
    .top-search {
      width: 44px;
      height: 44px;
      flex: 0 0 44px;
      justify-content: center;
      margin-left: auto;
      padding: 0;
      border: 0;
    }
    .search-icon {
      width: 16px;
      height: 16px;
      display: block;
      fill: none;
      stroke: currentColor;
      stroke-width: 1.5;
      stroke-linecap: round;
    }
    .search-label,
    .kbd {
      display: none;
    }
    .guest-state {
      display: none;
    }
    .console.voice-mode .wordmark,
    .console.voice-mode .top-search,
    .console.voice-mode .desktop-attachment,
    .console.voice-mode .voice-vm-pill {
      display: none;
    }
    .console.voice-mode .mode-pill {
      padding: 0 10px;
    }
    .console.voice-mode .voice-attachment {
      flex: 1;
      overflow: hidden;
    }
    .console.voice-mode .phone-attachment {
      display: block;
      overflow: hidden;
      color: var(--muted);
      font: 12px var(--font-mono);
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .console.voice-mode .leave-voice {
      min-width: 44px;
      height: 44px;
      padding: 0 8px;
    }
    .console .shell .inbox {
      flex: 0 0 100%;
      border-right: 0;
    }
    .console.mobile-transcript .inbox {
      display: none;
    }
    .console:not(.mobile-transcript) .detail {
      display: none;
    }
    .fold-button,
    .console .inbox .fold-rail {
      display: none;
    }
    .console .inbox .inbox-expanded {
      display: flex;
    }
    .mobile-home {
      display: block;
    }
    .mobile-detail-nav {
      min-height: 44px;
      display: flex;
      align-items: center;
      justify-content: space-between;
    }
    .mobile-back,
    .mobile-jump {
      min-height: 44px;
      border: 0;
      background: transparent;
    }
    .mobile-back {
      width: 44px;
      flex: 0 0 44px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 0;
      color: var(--muted);
    }
    .mobile-jump {
      width: 44px;
      flex: 0 0 44px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 0;
      color: var(--muted);
    }
    .mobile-back svg,
    .mobile-jump svg {
      width: 16px;
      height: 16px;
      fill: none;
      stroke: currentColor;
      stroke-width: 1.5;
      stroke-linecap: round;
    }
    .transcript-head {
      flex-basis: 56px;
      height: 56px;
      gap: 0;
      padding: 0 4px 0 0;
    }
    .session-title {
      flex: 1;
      font-size: 15px;
      line-height: 1.15;
    }
    .session-mobile-meta {
      display: block;
      margin-top: 3px;
      overflow: hidden;
      color: var(--muted);
      font-size: 12px;
      font-weight: 400;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .transcript-head .pill,
    .transcript-head .seg {
      display: none;
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
      border-top: 1px solid var(--line-strong);
      border-radius: 8px 8px 0 0;
    }
    .new-panel .model-picker select {
      min-height: 44px;
    }
    .new-button,
    .quiet-button,
    .primary-button {
      min-height: 44px;
    }
    .row,
    .row.search-result {
      min-height: 60px;
    }
    .new-panel select,
    .hist {
      min-height: 44px;
    }
    .walkthrough-page {
      padding-left: 16px;
      padding-right: 16px;
    }
    .composer {
      padding: 12px 12px calc(12px + env(safe-area-inset-bottom, 34px));
    }
    .box {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 44px;
      grid-template-rows: 44px 34px;
      column-gap: 8px;
      align-items: stretch;
    }
    .composer .box textarea {
      grid-column: 1;
      grid-row: 1;
      width: 100%;
      min-height: 44px;
      height: 44px;
      padding: 10px 12px;
      font-size: 15px;
    }
    .bar {
      display: contents;
    }
    .model-picker.composer-model {
      grid-column: 1;
      grid-row: 2;
      align-self: center;
      justify-self: start;
      margin-left: 4px;
    }
    .model-picker.composer-model select {
      min-height: 44px;
    }
    .composer-hint {
      /* A phone has no ⌘↵. */
      display: none;
    }
    .composer-submit {
      grid-column: 2;
      grid-row: 1;
      width: 44px;
      height: 44px;
      flex-basis: 44px;
      border-radius: 6px;
    }
    /* A desktop fold must not blank the phone column. These outrank the fold
       rules above by source order at equal specificity. */
    :global(html[data-agents-rail="folded"]) .console .inbox,
    .console.rail-folded .inbox {
      flex-basis: 100%;
    }
    :global(html[data-agents-rail="folded"]) .console .fold-rail,
    .console.rail-folded .fold-rail {
      display: none;
    }
    :global(html[data-agents-rail="folded"]) .console .inbox-expanded,
    .console.rail-folded .inbox-expanded {
      display: flex;
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
