<script>
  import RunView from "./RunView.svelte";
  import Turns from "./Turns.svelte";
  import WalkthroughNarrative from "./WalkthroughNarrative.svelte";
  import VoiceAskCard from "./VoiceAskCard.svelte";
  import { relativeTime } from "./run-history.js";
  import { vmState } from "./status.js";
  import { walkthroughTurns } from "./session-view.js";
  import { RUN_LEXICON as P } from "./run-lexicon.js";
  import {
    cardPhase,
    exchangeCount,
    renderSummoningCall,
    renderWireCall,
  } from "./companion/stage.js";

  let {
    stage,
    rows = [],
    sessionDetail = null,
    vms = {},
    runDetails = {},
    now = Date.now(),
    renderedPending = {},
    onPin = () => {},
    onDismiss = () => {},
    onSend = async () => {},
    onAnswered = () => {},
  } = $props();

  let wireListEl = $state(null);

  let wireOpen = $state(false);
  const session = $derived(sessionDetail?.session ?? null);
  const turns = $derived(sessionDetail?.turns ?? []);
  const latestTurn = $derived(turns.length ? turns.at(-1) : null);
  const visibleCards = $derived(
    (stage?.cards ?? []).filter((card) => cardPhase(card, rows) !== "gone"),
  );
  // Only what the server recorded as spoken (extract_voice_summary already
  // falls back to the first sentence server-side); never synthesise a line
  // here, or an inferred surface reads as summoned (ADR 058).
  const voiceLine = $derived(
    latestTurn?.voice_summary ?? session?.voice_summary ?? "",
  );
  const model = $derived(
    latestTurn?.model || session?.model || P.labels.defaultModel,
  );
  const spokenAge = $derived(
    latestTurn?.created_at
      ? relativeTime(latestTurn.created_at, new Date(now))
      : P.labels.relativeNow,
  );
  const vm = $derived(
    session?.ember_session_id ? vms[session.ember_session_id] : null,
  );
  const attachedId = $derived(stage?.attachedSessionId ?? null);
  const walkthrough = $derived(walkthroughTurns(turns));

  function kindLabel(card) {
    if (card.kind === "ask") return P.labels.decisionKind;
    if (card.kind === "tool") return P.labels.toolKind;
    return (
      {
        run: P.labels.runKind,
        walkthrough: P.labels.walkthroughKind,
        transcript: P.labels.transcriptKind,
        vm: P.labels.vmKind,
      }[card.surface] ?? P.labels.toolKind
    );
  }

  function ageLabel(card) {
    if (card.answered) return P.labels.answered;
    const count = exchangeCount(card, rows);
    if (count === 0) return P.labels.relativeNow;
    if (count === 1) return P.labels.exchangeAgo;
    return P.labels.exchangesAgo.replace("{count}", String(count));
  }

  function wireTime(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return P.labels.unknownClock;
    return date.toLocaleTimeString("en-GB", {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
  }

  function walkthroughRows(card) {
    const match = String(card.ref).match(/(?:turn:)?(\d+)$/);
    if (!match) return walkthrough;
    const exact = walkthrough.filter(
      (turn) => Number(turn.seq) === Number(match[1]),
    );
    return exact.length ? exact : walkthrough;
  }

  $effect(() => {
    const currentRows = rows;
    if (!currentRows.length) return;
    wireListEl
      ?.querySelector("li.current")
      ?.scrollIntoView({ block: "nearest", behavior: "auto" });
  });
</script>

<div class="voice-workspace">
  <section class="stage" aria-label={P.labels.voiceCompanion}>
    <div class="spoken">
      {#if attachedId != null}
        <div class="spoken-meta">
          {model}
          {P.punct.dot}
          {P.labels.spoken}
          {spokenAge}
        </div>
        <p class:quiet={!voiceLine}>
          {voiceLine || P.labels.voiceSurfaceUnavailable}
        </p>
      {:else}
        <div class="spoken-meta">{P.labels.voiceCompanion}</div>
        <p class="quiet">{P.labels.waitingVoiceAttach}</p>
      {/if}
    </div>

    <div class="surfaces">
      {#each visibleCards as card, index (card.key)}
        {@const phase = cardPhase(card, rows)}
        <article
          class:front={index === 0}
          class:receded={phase === "receded"}
          class:pinned={card.pinned}
          class="surface-card"
        >
          <header class="card-head">
            <strong>{kindLabel(card)}</strong>
            <span class="badge">{renderSummoningCall(card)}</span>
            <span class="card-age">{ageLabel(card)}</span>
            <button
              class="icon-button pin"
              type="button"
              aria-label={P.labels.pinCard}
              aria-pressed={card.pinned}
              title={P.labels.pinCard}
              onclick={() => onPin(card.key)}
            >
              <svg viewBox="0 0 16 16" aria-hidden="true"
                ><path d="m5 2 6 1-1.5 3 2.5 2.5-3.5 1L7 14l-1-4-4-1 3-2.5z"
                ></path></svg
              >
            </button>
            <button
              class="icon-button"
              type="button"
              aria-label={P.labels.dismissCard}
              title={P.labels.dismissCard}
              onclick={() => onDismiss(card.key)}
            >
              <svg viewBox="0 0 16 16" aria-hidden="true"
                ><path d="m4 4 8 8m0-8-8 8"></path></svg
              >
            </button>
          </header>
          {#if card.kind === "ask"}
            <VoiceAskCard {card} sessionId={attachedId} {onSend} {onAnswered} />
          {:else if card.kind === "tool"}
            <div class="tool-row">{renderSummoningCall(card)}</div>
          {:else if card.surface === "run"}
            {@const runDetail = runDetails[card.ref]}
            {#if runDetail}
              <RunView
                compact
                run={runDetail.run}
                view={runDetail.view}
                sessions={runDetail.sessions}
                focus={card.focus}
              />
            {:else}
              <div class="surface-empty">{P.labels.loadingRun}</div>
            {/if}
          {:else if card.surface === "walkthrough"}
            <div class="walkthrough-body">
              {#each walkthroughRows(card) as turn (turn.seq)}
                <WalkthroughNarrative
                  sessionId={attachedId}
                  turnSeq={turn.seq}
                  walkthroughTurnCount={walkthroughRows(card).length}
                />
              {:else}
                <div class="surface-empty">
                  {P.labels.walkthroughUnavailableForSession}
                </div>
              {/each}
            </div>
          {:else if card.surface === "transcript"}
            <!-- renderedPending is the selected console session's partials
             (loadDetail / syncPendingPartials). When the voice-attached
             session is also selected, the live line and partial text match
             the session pane; otherwise pending rows still render from
             sessionDetail.pending_queue without those partials. -->
            <Turns
              detail={sessionDetail}
              selectedSession={session}
              {renderedPending}
              {vms}
              compact
            />
          {:else if card.surface === "vm"}
            <div class="vm-body">
              <span class="vm-pill"
                ><span
                  class:awake={vmState(session, vms) === "awake"}
                  class="vm-dot"
                ></span>{P.labels.vmWord}
                {vmState(session, vms)}</span
              >
              <span class="cp-state"
                >{vm?.cp_state ?? P.labels.voiceSurfaceUnavailable}</span
              >
            </div>
          {/if}
        </article>
      {/each}
    </div>
  </section>

  <aside class:open={wireOpen} class="wire" aria-label={P.labels.wire}>
    <header>
      <strong>{P.labels.wire}</strong>
      <span
        >{P.labels.session} #{attachedId ?? P.labels.unknownValue}
        {P.punct.dot}
        {rows.length}
        {P.labels.calls}</span
      >
      <button
        type="button"
        aria-label={P.labels.closeWire}
        onclick={() => (wireOpen = false)}
      >
        <svg viewBox="0 0 16 16" aria-hidden="true"
          ><path d="m4 4 8 8m0-8-8 8"></path></svg
        >
      </button>
    </header>
    <ol bind:this={wireListEl}>
      {#each rows as row, index (row.id)}
        <li class:current={index === rows.length - 1}>
          <time datetime={row.created_at}>{wireTime(row.created_at)}</time>
          <span>{renderWireCall(row)}</span>
        </li>
      {:else}
        <li class="wire-empty">{P.labels.noVoiceCalls}</li>
      {/each}
    </ol>
  </aside>

  <button
    class="wire-bar"
    type="button"
    aria-expanded={wireOpen}
    onclick={() => (wireOpen = true)}
  >
    {P.labels.wireCollapsed.replace("{count}", String(rows.length))}
  </button>
</div>

<style>
  .voice-workspace {
    min-width: 0;
    min-height: 0;
    flex: 1;
    display: flex;
    background: var(--panel-bg);
  }
  .stage {
    min-width: 0;
    min-height: 0;
    flex: 1;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  .spoken {
    padding: 28px 40px 24px;
    border-bottom: 1px solid var(--line);
  }
  .spoken-meta {
    color: var(--muted);
    font: var(--size-meta) var(--font-mono);
  }
  .spoken p {
    max-width: 760px;
    margin: 8px 0 0;
    color: var(--text);
    font-size: 20px;
    font-weight: 500;
    line-height: 1.35;
    text-wrap: pretty;
  }
  .spoken p.quiet {
    color: var(--muted);
  }
  .surfaces {
    min-height: 0;
    padding: 24px 40px 32px;
    display: flex;
    flex-direction: column;
    gap: 16px;
    overflow-y: auto;
    background: var(--page-bg);
  }
  .surface-card {
    flex: 0 0 auto;
    overflow: hidden;
    border: 1px solid var(--line);
    border-radius: 6px;
    background: var(--panel-bg);
  }
  .surface-card.front {
    border-color: var(--text);
    box-shadow: 0 0 0 1px var(--text);
  }
  .surface-card.receded {
    opacity: 0.5;
  }
  .surface-card.pinned {
    opacity: 1;
  }
  .card-head {
    height: 40px;
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 0 8px 0 14px;
    border-bottom: 1px solid var(--line);
  }
  .card-head strong {
    color: var(--text);
    font-size: 13px;
    font-weight: 600;
  }
  .badge {
    max-width: min(52%, 520px);
    overflow: hidden;
    padding: 3px 6px;
    border-radius: 3px;
    color: var(--muted);
    background: var(--page-bg);
    font: var(--size-meta) var(--font-mono);
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .card-age {
    margin-left: auto;
    color: var(--muted);
    font: var(--size-meta) var(--font-mono);
    white-space: nowrap;
  }
  .icon-button {
    width: 32px;
    height: 32px;
    flex: 0 0 32px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 0;
    border: 0;
    border-radius: var(--radius-md);
    color: var(--muted);
    background: transparent;
  }
  .icon-button:hover {
    color: var(--text);
    background: var(--hover);
  }
  .icon-button:focus-visible,
  .wire button:focus-visible,
  .wire-bar:focus-visible {
    outline: 2px solid var(--info);
    outline-offset: 2px;
  }
  .icon-button svg,
  .wire header button svg {
    width: 16px;
    height: 16px;
    fill: none;
    stroke: currentColor;
    stroke-width: 1.5;
    stroke-linecap: round;
    stroke-linejoin: round;
  }
  .icon-button.pin[aria-pressed="true"] svg {
    fill: currentColor;
  }
  .tool-row,
  .vm-body {
    min-height: 40px;
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 0 16px;
    color: var(--text-soft);
    font: 12px var(--font-mono);
  }
  .vm-pill {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 3px 7px;
    border: 1px solid var(--line);
    border-radius: var(--radius-pill);
  }
  .vm-dot {
    width: 6px;
    height: 6px;
    border-radius: var(--radius-circle);
    background: var(--dot-idle);
  }
  .vm-dot.awake {
    background: var(--ok);
  }
  .cp-state {
    color: var(--muted);
  }
  .walkthrough-body {
    padding: 16px;
  }
  .surface-empty {
    padding: 20px 16px;
    color: var(--muted);
    font-size: var(--size-detail);
  }
  .wire {
    width: 320px;
    flex: 0 0 320px;
    min-height: 0;
    display: flex;
    flex-direction: column;
    border-left: 1px solid var(--line);
    background: var(--page-bg);
  }
  .wire header {
    height: 44px;
    flex: 0 0 44px;
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 0 12px;
    border-bottom: 1px solid var(--line);
  }
  .wire header strong {
    color: var(--text);
    font-size: 13px;
    font-weight: 600;
  }
  .wire header span {
    margin-left: auto;
    color: var(--muted);
    font: var(--size-meta) var(--font-mono);
    white-space: nowrap;
  }
  .wire header button {
    display: none;
    width: 32px;
    height: 32px;
    align-items: center;
    justify-content: center;
    padding: 0;
    border: 0;
    color: var(--muted);
    background: transparent;
  }
  .wire ol {
    min-height: 0;
    margin: 0;
    padding: 0;
    overflow-y: auto;
    list-style: none;
  }
  .wire li {
    min-height: 44px;
    display: grid;
    grid-template-columns: 44px minmax(0, 1fr);
    align-items: center;
    padding: 0 12px;
    color: var(--text);
    font: 600 12px var(--font-mono);
    overflow-wrap: anywhere;
  }
  .wire li.current {
    background: var(--panel-bg);
  }
  .wire time {
    color: var(--muted);
    font-weight: 400;
    font-variant-numeric: tabular-nums;
  }
  .wire li.wire-empty {
    display: flex;
    color: var(--muted);
    font-weight: 400;
  }
  .wire-bar {
    display: none;
  }
  @media (prefers-reduced-motion: no-preference) {
    .surface-card {
      animation: arrive 180ms ease-out;
    }
  }
  @keyframes arrive {
    from {
      opacity: 0;
      transform: translateY(6px);
    }
  }
  @media (max-width: 760px) {
    .spoken {
      padding: 18px;
    }
    .spoken p {
      font-size: 18px;
    }
    .surfaces {
      padding: 16px 16px 60px;
    }
    .card-head {
      padding-left: 10px;
      gap: 6px;
    }
    .badge {
      max-width: 38%;
    }
    .card-age {
      display: none;
    }
    .wire {
      position: fixed;
      z-index: 3;
      right: 0;
      bottom: 0;
      left: 0;
      width: 100%;
      height: min(70dvh, 560px);
      min-height: 0;
      display: none;
      border-top: 1px solid var(--line-strong);
      border-left: 0;
      border-radius: 8px 8px 0 0;
      box-shadow: var(--panel-shadow);
    }
    .wire.open {
      display: flex;
    }
    .wire header button {
      display: inline-flex;
    }
    .wire-bar {
      position: fixed;
      z-index: 2;
      right: 0;
      bottom: 0;
      left: 0;
      height: 44px;
      display: block;
      border: 0;
      border-top: 1px solid var(--line);
      border-radius: 0;
      color: var(--muted);
      background: var(--page-bg);
      font: 12px var(--font-mono);
    }
  }
</style>
