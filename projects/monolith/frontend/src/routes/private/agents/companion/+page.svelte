<script>
  import { onMount } from "svelte";
  import { periodForHour } from "$lib/private/period.js";
  import { vmState } from "../status.js";
  import { applyLedgerRows, dismissCard, emptyStage } from "./stage.js";
  import "../agents-theme.css";

  const POLL_MS = 2000;
  const STALE_MS = 60_000;
  const STORAGE_ID = "voice-companion-id";
  const STORAGE_SINCE = "voice-companion-since";

  let companionId = $state(null);
  let since = $state(0);
  let stage = $state(emptyStage());
  let wireRows = $state([]);
  let sessionDetail = $state(null);
  let vms = $state({});
  let lastPollOkAt = $state(null);
  let startedAt = $state(Date.now());
  let now = $state(Date.now());
  let clock = $state(new Date());
  let wireEl = $state(null);
  let period = $derived(periodForHour(clock.getHours()));

  $effect(() => {
    if (typeof document !== "undefined") {
      document.documentElement.setAttribute("data-agents-period", period);
    }
  });

  $effect(() => {
    wireRows.length;
    if (wireEl) wireEl.scrollTop = wireEl.scrollHeight;
  });

  const attached = $derived(stage.attachedSessionId != null);
  const pollStale = $derived(now - (lastPollOkAt ?? startedAt) > STALE_MS);
  const stateLabel = $derived(
    pollStale ? "stale · no poll" : attached ? "attached" : "unattached",
  );
  const session = $derived(sessionDetail?.session ?? null);
  const latestTurn = $derived.by(() => {
    const turns = sessionDetail?.turns ?? [];
    return turns.length ? turns[turns.length - 1] : null;
  });
  const voiceText = $derived(
    !attached
      ? "Nothing attached. The stage is dark."
      : (latestTurn?.voice_summary ?? session?.voice_summary ?? ""),
  );
  const voiceDim = $derived(!attached || !voiceText);
  const youText = $derived(latestTurn?.prompt ? String(latestTurn.prompt) : "");
  const vmLabel = $derived(session ? `vm ${vmState(session, vms)}` : "");
  const repoChip = $derived.by(() => {
    if (!session) return "";
    const repo = session.repo ?? "";
    const branch = session.branch ?? "";
    if (repo && branch) return `${repo} · ${branch}`;
    return repo || branch;
  });

  function readStored(key) {
    try {
      return sessionStorage.getItem(key);
    } catch {
      return null;
    }
  }

  function writeStored(key, value) {
    try {
      if (value == null) sessionStorage.removeItem(key);
      else sessionStorage.setItem(key, String(value));
    } catch {
      // sessionStorage blocked
    }
  }

  async function registerCompanion() {
    const stored = readStored(STORAGE_ID);
    const body = stored ? { companion_id: stored } : {};
    const response = await fetch("/agents/companion", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(10000),
    });
    if (!response.ok) throw new Error("companion register failed");
    const data = await response.json();
    const nextId = data?.companion_id;
    if (!nextId) throw new Error("companion register failed");
    companionId = nextId;
    writeStored(STORAGE_ID, nextId);
    if (stored && stored === nextId) {
      const storedSince = Number(readStored(STORAGE_SINCE) ?? "0");
      since = Number.isFinite(storedSince) ? storedSince : 0;
    } else {
      since = 0;
      writeStored(STORAGE_SINCE, "0");
    }
  }

  async function loadSession(sessionId) {
    try {
      const [sessionRes, vmRes] = await Promise.all([
        fetch(`/agents/session/${encodeURIComponent(sessionId)}`, {
          signal: AbortSignal.timeout(10000),
        }),
        fetch("/agents/vms", { signal: AbortSignal.timeout(10000) }),
      ]);
      if (sessionRes.ok) sessionDetail = await sessionRes.json();
      if (vmRes.ok) {
        const body = await vmRes.json();
        vms = body?.vms ?? body ?? {};
      }
    } catch {
      // Spoken strip stays on the last good snapshot.
    }
  }

  function forgetCompanion() {
    companionId = null;
    since = 0;
    stage = emptyStage();
    wireRows = [];
    sessionDetail = null;
    writeStored(STORAGE_ID, null);
    writeStored(STORAGE_SINCE, "0");
  }

  async function pollLedger() {
    if (!companionId) return;
    const response = await fetch(
      `/agents/companion/${encodeURIComponent(companionId)}/ledger?since=${since}`,
      { signal: AbortSignal.timeout(10000) },
    );
    if (response.status === 404) {
      forgetCompanion();
      return "unknown";
    }
    if (!response.ok) return;
    const rows = await response.json();
    lastPollOkAt = Date.now();
    if (!Array.isArray(rows)) return;
    if (rows.length > 0) {
      wireRows = [...wireRows, ...rows];
      stage = applyLedgerRows(stage, rows);
      const maxId = Math.max(...rows.map((row) => Number(row.id)));
      if (Number.isFinite(maxId) && maxId > since) {
        since = maxId;
        writeStored(STORAGE_SINCE, String(since));
      }
    }
    if (stage.attachedSessionId != null) {
      await loadSession(stage.attachedSessionId);
    }
  }

  onMount(() => {
    startedAt = Date.now();
    now = Date.now();
    let cancelled = false;
    let inFlight = false;

    async function tick() {
      if (cancelled || inFlight) return;
      inFlight = true;
      now = Date.now();
      try {
        if (!companionId) await registerCompanion();
        if (cancelled) return;
        const result = await pollLedger();
        if (result === "unknown" && !cancelled) {
          await registerCompanion();
          if (!cancelled) await pollLedger();
        }
      } catch {
        // Staleness is the visible signal; the next tick retries.
      } finally {
        inFlight = false;
      }
    }

    function onVisibilityChange() {
      if (document.visibilityState === "visible") tick();
    }

    tick();
    const pollId = setInterval(tick, POLL_MS);
    const clockId = setInterval(() => {
      clock = new Date();
      now = Date.now();
    }, 1000);
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      cancelled = true;
      clearInterval(pollId);
      clearInterval(clockId);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  });

  function onDismiss(key) {
    stage = dismissCard(stage, key);
  }

  function cardEyebrow(card) {
    if (card.kind === "ask") return `decision · ${card.ref}`;
    return `${card.surface} · ${card.ref}`;
  }

  function cardHref(card) {
    if (card.surface === "run") {
      return `/agents?run=${encodeURIComponent(card.ref)}`;
    }
    if (stage.attachedSessionId != null) {
      return `/agents?session=${encodeURIComponent(stage.attachedSessionId)}`;
    }
    return "/agents";
  }

  function cardLinkLabel(card) {
    if (card.surface === "run") return "open the run · /private/agents";
    if (card.surface === "walkthrough") {
      return "continues in the full walkthrough view · /private/agents";
    }
    if (card.surface === "transcript") {
      return "open the transcript · /private/agents";
    }
    return "open in /private/agents";
  }

  function wireTime(createdAt) {
    if (createdAt == null) return "--:--:--";
    const date = createdAt instanceof Date ? createdAt : new Date(createdAt);
    if (Number.isNaN(date.getTime())) return String(createdAt);
    return date.toLocaleTimeString("en-GB", { hour12: false });
  }

  function wireArgs(row) {
    const payload =
      row?.payload && typeof row.payload === "object" ? row.payload : {};
    if (row.call === "attach") return `{session_id: ${payload.session_id}}`;
    if (row.call === "show") {
      const parts = [`surface: "${payload.surface}"`, `ref: "${payload.ref}"`];
      if (payload.focus) parts.push(`focus: "${payload.focus}"`);
      return `{${parts.join(", ")}}`;
    }
    if (row.call === "ask") {
      return `{ref: "${payload.ref}", question: "${payload.question ?? ""}"}`;
    }
    if (row.call === "dismiss") {
      return payload.surface ? `{surface: "${payload.surface}"}` : "{}";
    }
    try {
      return JSON.stringify(payload);
    } catch {
      return "";
    }
  }
</script>

<svelte:head>
  <title>voice companion</title>
</svelte:head>

<main class="console companion-page">
  <div class="companion">
    <div class="attach" class:on={attached && !pollStale}>
      <span class="vbars" aria-hidden="true"><i></i><i></i><i></i><i></i></span>
      <span class="eyebrow">voice companion</span>
      <span class="chips">
        {#if attached}
          <span class="chip"
            >session {session?.id ?? stage.attachedSessionId}</span
          >
          {#if session?.model}<span class="chip">{session.model}</span>{/if}
          {#if vmLabel}<span class="chip vm">{vmLabel}</span>{/if}
          {#if repoChip}<span class="chip">{repoChip}</span>{/if}
        {/if}
      </span>
      <span class="state"
        ><span class="dot"></span><span>{stateLabel}</span></span
      >
    </div>

    <div class="spoken">
      <p class="you">
        {#if youText}you · <b>&ldquo;{youText}&rdquo;</b>{/if}
      </p>
      <p class="line" class:dim={voiceDim} aria-live="polite">
        {voiceText}
      </p>
    </div>

    <div class="stage">
      {#if stage.cards.length === 0}
        <div class="empty">
          <div class="glyph">&#9678;</div>
          {#if attached}
            <p>attached · nothing on the stage</p>
            <p class="mono">waiting on voice_ui_show</p>
          {:else}
            <p>no voice session attached</p>
            <p class="mono">waiting on voice_ui_attach</p>
          {/if}
        </div>
      {:else}
        {#each stage.cards as card (card.key)}
          <article
            class="card enter"
            class:attn-card={card.kind === "ask"}
            data-key={card.key}
          >
            <header class="card-head">
              <span class="eyebrow">{cardEyebrow(card)}</span>
              <span class="summon">via voice_ui_{card.call}</span>
              <span class="card-acts">
                <button
                  type="button"
                  class="b-dis"
                  title="dismiss"
                  onclick={() => onDismiss(card.key)}>&times;</button
                >
              </span>
            </header>
            <div class="card-body">
              {#if card.kind === "ask"}
                <p class="card-title">{card.question || card.ref}</p>
                {#if card.options?.length}
                  <div class="ask-acts">
                    <!-- Ask resolution through agent_session_send is the
                         follow-up slice; these option buttons stay inert. -->
                    {#each card.options as option, index (option)}
                      <button
                        type="button"
                        class={index === 0 ? "btn-ink" : "btn-quiet"}
                        >{option}</button
                      >
                    {/each}
                  </div>
                {/if}
              {:else}
                <p class="card-title">{card.ref}</p>
                <p class="card-meta">
                  {card.surface}{#if card.focus}
                    · {card.focus}{/if}
                </p>
                <p class="walklink">
                  <a href={cardHref(card)}>{cardLinkLabel(card)}</a>
                </p>
              {/if}
            </div>
          </article>
        {/each}
      {/if}
    </div>

    <div class="wire">
      <span class="eyebrow">the wire · mcp calls driving this view</span>
      <div class="wire-lines" bind:this={wireEl}>
        {#if wireRows.length === 0}
          <div class="none">no calls yet</div>
        {:else}
          {#each wireRows as row (row.id)}
            <div>
              <span class="t">{wireTime(row.created_at)}</span>
              <span class="tool">voice_ui_{row.call}</span>
              <span class="args">{wireArgs(row)}</span>
            </div>
          {/each}
        {/if}
      </div>
    </div>
  </div>
</main>

<style>
  .companion-page {
    min-height: 100dvh;
    background: var(--page-bg);
    color: var(--text);
    font: var(--size-body) / 1.45 var(--font-ui);
    padding: 20px;
    box-sizing: border-box;
  }
  .companion {
    max-width: 1060px;
    margin: 0 auto;
    background: var(--page-bg);
    color: var(--text);
    border: 1px solid var(--line);
    border-radius: var(--radius-lg);
    overflow: hidden;
    display: grid;
    grid-template-rows: auto auto 1fr auto;
    height: clamp(560px, 74vh, 720px);
  }
  .eyebrow {
    font: 11.5px var(--font-mono);
    letter-spacing: 0.13em;
    text-transform: uppercase;
    color: var(--muted);
  }
  button {
    font-family: var(--font-ui);
    cursor: pointer;
  }
  button:focus-visible {
    outline: 2px solid var(--info);
    outline-offset: 2px;
  }

  .attach {
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
    background: var(--panel-bg);
    border-bottom: 1px solid var(--line);
    padding: 12px 18px;
    min-height: 32px;
  }
  .vbars {
    display: inline-flex;
    gap: 2.5px;
    align-items: flex-end;
    height: 14px;
    width: 18px;
  }
  .vbars i {
    width: 2.5px;
    background: var(--dot-idle);
    border-radius: 1px;
    height: 4px;
    display: block;
  }
  .attach .chips {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    align-items: center;
  }
  .chip {
    font: 11.5px var(--font-mono);
    color: var(--text-soft);
    background: var(--code-bg);
    border: 1px solid var(--line);
    border-radius: 3px;
    padding: 2px 8px;
    white-space: nowrap;
  }
  .chip.vm {
    color: var(--ok);
    border-color: var(--ok-soft);
    background: var(--ok-soft);
  }
  .attach .state {
    margin-left: auto;
    display: inline-flex;
    align-items: center;
    gap: 7px;
    font: 11.5px var(--font-mono);
    letter-spacing: 0.13em;
    text-transform: uppercase;
    color: var(--muted);
  }
  .attach .state .dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--dot-idle);
  }
  .attach.on .state .dot {
    background: var(--ok);
  }
  @media (prefers-reduced-motion: no-preference) {
    .attach.on .state .dot {
      animation: pulse 1.6s ease-in-out infinite;
    }
  }
  @keyframes pulse {
    50% {
      opacity: 0.35;
    }
  }

  .spoken {
    padding: 12px 18px 13px;
    border-bottom: 1px solid var(--line);
    background: var(--panel-bg);
    min-height: 44px;
  }
  .spoken .you {
    font: 11.5px var(--font-mono);
    color: var(--muted);
    margin: 0 0 3px;
    min-height: 1.2em;
  }
  .spoken .you b {
    color: var(--text-soft);
    font-weight: 500;
  }
  .spoken .line {
    margin: 0;
    max-width: 65ch;
    font-size: 14px;
    color: var(--text);
  }
  .spoken .line.dim {
    color: var(--muted);
  }

  .stage {
    padding: 18px;
    overflow-y: auto;
    display: flex;
    flex-wrap: wrap;
    gap: 16px;
    align-content: flex-start;
  }
  .empty {
    margin: auto;
    text-align: center;
    color: var(--muted);
  }
  .empty .glyph {
    font-size: 26px;
    opacity: 0.5;
    margin-bottom: 10px;
  }
  .empty p {
    margin: 0 0 6px;
  }
  .empty .mono {
    font: 11.5px var(--font-mono);
  }

  .card {
    background: var(--panel-bg);
    border: 1px solid var(--line);
    border-radius: 6px;
    width: min(430px, 100%);
    align-self: flex-start;
  }
  .card.attn-card {
    border-color: var(--attn);
    box-shadow: 0 0 0 1px var(--attn-soft);
  }
  @media (prefers-reduced-motion: no-preference) {
    .card.enter {
      animation: conjure 0.55s ease-out;
    }
  }
  @keyframes conjure {
    from {
      opacity: 0;
      transform: translateY(10px);
    }
    to {
      opacity: 1;
      transform: translateY(0);
    }
  }
  .card-head {
    display: flex;
    align-items: baseline;
    gap: 10px;
    padding: 10px 14px 9px;
    border-bottom: 1px solid var(--line);
  }
  .card-head .summon {
    font: 11px var(--font-mono);
    color: var(--info);
    margin-left: auto;
    white-space: nowrap;
  }
  .card-acts {
    display: inline-flex;
    gap: 2px;
  }
  .card-acts button {
    font: 11.5px var(--font-mono);
    color: var(--muted);
    background: none;
    border: none;
    border-radius: 3px;
    padding: 1px 6px;
  }
  .card-acts button:hover {
    background: var(--hover);
    color: var(--text);
  }
  .card-body {
    padding: 13px 14px 14px;
  }
  .card-title {
    font-size: 15px;
    font-weight: 600;
    margin: 0 0 3px;
  }
  .card-meta {
    font: 12.5px var(--font-mono);
    color: var(--muted);
    margin: 0 0 12px;
  }
  .walklink,
  .walklink a {
    margin: 10px 0 0;
    font: 11.5px var(--font-mono);
    color: var(--muted);
    text-decoration: none;
  }
  .walklink a:hover {
    color: var(--info);
  }
  .ask-acts {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    margin-top: 12px;
  }
  .btn-ink {
    background: var(--ink);
    color: var(--ink-text);
    border: 1px solid var(--ink);
    border-radius: 4px;
    padding: 8px 16px;
    font-size: 13px;
    font-weight: 600;
  }
  .btn-quiet {
    background: var(--panel-bg);
    color: var(--text);
    border: 1px solid var(--line-strong);
    border-radius: 4px;
    padding: 8px 16px;
    font-size: 13px;
  }

  .wire {
    border-top: 1px solid var(--line);
    background: var(--panel-bg);
    padding: 9px 18px 12px;
  }
  .wire .eyebrow {
    display: block;
    margin-bottom: 6px;
  }
  .wire-lines {
    max-height: 92px;
    overflow-y: auto;
  }
  .wire-lines div {
    font: 11.5px / 1.7 var(--font-mono);
    color: var(--text-soft);
    white-space: nowrap;
    overflow-x: auto;
  }
  .wire-lines .t {
    color: var(--muted);
    font-variant-numeric: tabular-nums;
    margin-right: 10px;
  }
  .wire-lines .tool {
    color: var(--info);
  }
  .wire-lines .args {
    color: var(--muted);
  }
  .wire-lines .none {
    color: var(--muted);
    font-style: italic;
  }

  @media (max-width: 720px) {
    .companion {
      height: auto;
      min-height: 640px;
    }
    .attach .state {
      margin-left: 0;
      width: 100%;
    }
    .card {
      width: 100%;
    }
  }
  @media (prefers-reduced-motion: reduce) {
    .companion *,
    .companion,
    .empty {
      animation: none !important;
      transition: none !important;
    }
  }
</style>
