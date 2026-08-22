<script>
  import { renderAgentMarkdown, resultWithoutTrailer } from "./markdown.js";
  import { clockTime } from "./run-history.js";
  import { vmRunning } from "./status.js";
  import { RUN_LEXICON as P } from "./run-lexicon.js";
  import { workspaceRecoveryMessage } from "./workspace-recovery.js";
  import SessionWalkthrough from "./SessionWalkthrough.svelte";

  let {
    detail = null,
    selectedSession = null,
    renderedPending = {},
    vms = {},
    element = $bindable(null),
    compact = false,
  } = $props();

  const CLEAN_TERMINAL_REASONS = new Set(["completed", "end_turn", "stop"]);

  function cost(value) {
    const amount = Number(value || 0);
    if (!(amount > 0)) return "";
    return amount >= 0.01 ? `$${amount.toFixed(2)}` : `$${amount.toFixed(4)}`;
  }

  function turnFailed(turn) {
    return Boolean(
      turn?.terminal_reason &&
      !CLEAN_TERMINAL_REASONS.has(turn.terminal_reason),
    );
  }

  function turnProtocol(turn) {
    if (turn?.prompt_intent == null) return "";
    const prefix = `${turn.prompt_intent}\n`;
    if (!turn.prompt?.startsWith(prefix)) return "";
    return turn.prompt.slice(prefix.length);
  }

  function compactInput(input) {
    if (input == null) return "";
    const text = typeof input === "string" ? input : JSON.stringify(input);
    return text.length > 110 ? `${text.slice(0, 110)}…` : text;
  }

  function activityParts(activity) {
    if (typeof activity === "string") return { verb: activity, detail: "" };
    if (!activity || typeof activity !== "object") {
      return { verb: P.labels.stepWord, detail: "" };
    }
    const kind = String(
      activity.type || activity.tool || activity.name || "",
    ).toLowerCase();
    if (kind === "edit" || kind === "write") {
      return { verb: kind, detail: activity.file_path || activity.path || "" };
    }
    if (kind === "bash" || kind === "shell") {
      return {
        verb: P.labels.run,
        detail: activity.command || compactInput(activity.input),
      };
    }
    if (activity.name) {
      return {
        verb: String(activity.name),
        detail: compactInput(activity.input),
      };
    }
    return {
      verb: kind || P.labels.stepWord,
      detail: compactInput(activity.input),
    };
  }

  function stepCountLabel(count) {
    return count === 1 ? P.labels.stepWord : P.labels.stepsWord;
  }

  function activityLine(activity) {
    const { verb, detail: activityDetail } = activityParts(activity);
    return activityDetail ? `${verb} ${activityDetail}` : verb;
  }

  function liveStateLabel(entry, index) {
    if (entry.claimed_by_replica) return "working";
    return index === 0 ? "starting" : "waiting";
  }
</script>

<div class:compact class="turns" bind:this={element}>
  <div class="turns-inner">
    {#each detail?.turns ?? [] as turn (turn.seq)}
      {@const hasIntent =
        turn.prompt_intent !== null && turn.prompt_intent !== undefined}
      {@const degradedCause = workspaceRecoveryMessage(turn)}
      <div class="turn-group">
        <article class="turn">
          <div class="meta prompt-meta mono">
            <span class="meta-role">{P.labels.youRole}</span>
            <span>{clockTime(turn.created_at)}</span>
          </div>
          <div class="prompt">
            {#if hasIntent}
              <div class="prompt-text intent-prompt">
                <span class="intent-label">{P.labels.promptIntent}</span>
                <div>{turn.prompt_intent}</div>
              </div>
              {#if turnProtocol(turn)}
                <details class="prompt-protocol">
                  <summary>{P.labels.viewProtocol}</summary>
                  <pre>{turnProtocol(turn)}</pre>
                </details>
              {/if}
            {:else}
              <div class="prompt-text">{turn.prompt}</div>
            {/if}
          </div>
          <div class="meta response-meta mono">
            <span
              >{turn.model ||
                selectedSession?.model ||
                P.labels.defaultModel}</span
            >
            <span>{clockTime(turn.created_at)}</span>
            {#if cost(turn.cost_usd)}
              <span>{P.punct.dot} {cost(turn.cost_usd)}</span>
            {/if}
            {#if turn.stop_reason && !CLEAN_TERMINAL_REASONS.has(turn.stop_reason)}
              <span class="meta-trailing">{turn.stop_reason}</span>
            {/if}
            {#if turnFailed(turn)}
              <span class="badge-failed">{P.labels.turnFailed}</span>
            {/if}
          </div>
          {#if turn.usage?.activities?.length}
            <details class="steps">
              <summary>
                <svg viewBox="0 0 12 12" aria-hidden="true">
                  <path d="m4.25 2.5 3.5 3.5-3.5 3.5"></path>
                </svg>
                {turn.usage.activities.length}
                {stepCountLabel(turn.usage.activities.length)}
              </summary>
              <ol class="step-list">
                {#each turn.usage.activities as activity}
                  <li>
                    <span class="step-verb">{activityParts(activity).verb}</span
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
            <pre class="turn-error">{resultWithoutTrailer(turn) ||
                P.labels.turnFailedWithoutOutput}</pre>
          {:else if turn.result_text}
            <div class="result-md">
              {@html renderAgentMarkdown(resultWithoutTrailer(turn))}
            </div>
          {/if}
          {#if selectedSession?.repo && !compact}
            <SessionWalkthrough
              sessionId={selectedSession.id}
              turnSeq={turn.seq}
              model={turn.model ||
                selectedSession.model ||
                P.labels.defaultModel}
            />
          {/if}
        </article>
        {#if degradedCause}
          <div
            class="turn-degraded"
            title={P.workspaceRecoveryCauses[degradedCause]}
          >
            {P.labels.workspaceRecovery}
          </div>
        {/if}
      </div>
    {/each}

    {#each detail?.pending_queue ?? [] as entry, index (entry.seq)}
      {@const partial = renderedPending[entry.seq]}
      {@const state = liveStateLabel(entry, index)}
      <article class="turn live">
        <div class="meta prompt-meta mono">
          <span class="meta-role">{P.labels.youRole}</span>
          <span>{clockTime(entry.created_at)}</span>
        </div>
        <div class="prompt"><div class="prompt-text">{entry.prompt}</div></div>
        <div class="meta response-meta mono">
          <span
            >{entry.model ||
              selectedSession?.model ||
              P.labels.defaultModel}</span
          >
          <span>{clockTime(entry.created_at)}</span>
        </div>
        {#if state === "working"}
          <details class="steps">
            <summary>
              <svg viewBox="0 0 12 12" aria-hidden="true">
                <path d="m4.25 2.5 3.5 3.5-3.5 3.5"></path>
              </svg>
              {partial?.partial_activities?.length ?? 0}
              {stepCountLabel(partial?.partial_activities?.length ?? 0)}
              {P.punct.dot}
              {P.labels.runningStep}
            </summary>
            {#if partial?.partial_activities?.length}
              <ol class="step-list" aria-label={P.labels.agentActivity}>
                {#each partial.partial_activities as activity}
                  <li>
                    <span class="step-verb">{activityParts(activity).verb}</span
                    >
                    <span class="step-detail"
                      >{activityParts(activity).detail}</span
                    >
                  </li>
                {/each}
              </ol>
            {/if}
          </details>
        {/if}
        <div class={`live-line ${state === "working" ? "" : "quiet"}`}>
          <span class="live-dot" aria-hidden="true"></span>
          {#if state === "working"}
            {#if partial?.partial_activities?.length}
              <span class="live-latest"
                >{activityLine(partial.partial_activities.at(-1))}</span
              >
            {:else if partial?.partial_text}
              <span class="live-latest">{P.labels.working}</span>
            {:else if vmRunning(selectedSession, vms)}
              <span class="live-latest">{P.labels.startingAgent}</span>
            {:else}
              <span class="live-latest">{P.labels.wakingVm}</span>
            {/if}
          {:else if state === "starting"}
            <span class="live-latest">{P.labels.startingUp}</span>
          {:else}
            <span class="live-latest">{P.labels.waitingForTurn}</span>
          {/if}
        </div>
        {#if partial?.partial_text}
          <div class="result-md">
            {@html renderAgentMarkdown(partial.partial_text)}
          </div>
        {/if}
      </article>
    {/each}

    {#if !(detail?.turns ?? []).length && !(detail?.pending_queue ?? []).length}
      <div class="empty transcript-empty">
        {detail ? P.labels.noTurnsYet : P.labels.loadingSession}
      </div>
    {/if}
  </div>
</div>

<style>
  .turns {
    flex: 1;
    padding: 24px 28px 20px;
    overflow: auto;
  }
  .turns.compact {
    max-height: 420px;
    padding: 16px;
  }
  .turns-inner {
    max-width: 720px;
    margin: 0 auto;
    display: flex;
    flex-direction: column;
    gap: 28px;
  }
  .turn {
    padding: 0;
  }
  .meta {
    display: flex;
    align-items: baseline;
    gap: 6px;
    color: var(--muted);
    font: 11.5px/1.4 var(--font-mono);
  }
  .meta-role {
    color: var(--text-soft);
    font: 600 12px var(--font-ui);
  }
  .prompt-meta {
    margin-bottom: 6px;
  }
  .response-meta {
    margin-top: 12px;
  }
  .meta-trailing,
  .badge-failed {
    margin-left: auto;
  }
  .meta-trailing + .badge-failed {
    margin-left: 0;
  }
  .prompt {
    padding: 12px 14px;
    border: 0;
    border-radius: 6px;
    background: var(--code-bg);
    font-size: 14px;
    line-height: 1.5;
    white-space: pre-wrap;
  }
  .prompt-text {
    min-width: 0;
    color: var(--text);
    overflow-wrap: anywhere;
  }
  .steps {
    margin: 8px 0 0;
  }
  .steps summary {
    width: fit-content;
    min-height: 28px;
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 0 9px 0 7px;
    border: 1px solid var(--line);
    border-radius: var(--radius-pill);
    color: var(--text-soft);
    font: 12px var(--font-mono);
    list-style: none;
    cursor: pointer;
    user-select: none;
  }
  .steps summary::-webkit-details-marker {
    display: none;
  }
  .steps summary svg {
    width: 12px;
    height: 12px;
    fill: none;
    stroke: currentColor;
    stroke-width: 1.5;
    stroke-linecap: round;
    stroke-linejoin: round;
    transition: transform 120ms ease;
  }
  .steps[open] summary svg {
    transform: rotate(90deg);
  }
  .steps summary:hover {
    color: var(--text);
  }
  .steps summary:focus-visible {
    outline: 2px solid var(--info);
    outline-offset: 2px;
  }
  .step-list {
    margin: 8px 0 0 6px;
    padding: 0 0 0 12px;
    border-left: 2px solid var(--line);
    list-style: none;
    display: grid;
    gap: 4px;
  }
  .step-list li {
    min-width: 0;
    display: grid;
    grid-template-columns: 72px minmax(0, 1fr);
    gap: 8px;
    color: var(--text-soft);
    font: 12.5px var(--font-mono);
  }
  .step-verb {
    color: var(--text);
    font-weight: 600;
  }
  .step-detail {
    color: var(--muted);
    overflow-wrap: anywhere;
  }
  .live-line {
    min-width: 0;
    display: flex;
    align-items: center;
    gap: 8px;
    margin: 12px 0 0;
    color: var(--ok);
    font: var(--size-body-mono) var(--font-mono);
  }
  .live-line.quiet {
    color: var(--muted);
  }
  .live-dot {
    width: 6px;
    height: 6px;
    flex: 0 0 6px;
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
    font-size: 14px;
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
    margin: 16px 0 8px;
    color: var(--text);
    font-size: var(--size-title);
  }
  .result-md :global(h3) {
    font-size: var(--size-body);
  }
  .result-md :global(ul),
  .result-md :global(ol) {
    margin: 8px 0;
    padding-left: 22px;
  }
  .result-md :global(li) {
    margin: 4px 0;
  }
  .result-md :global(code) {
    padding: 2px 4px;
    border-radius: 3px;
    background: var(--code-bg);
    font: 12.5px var(--font-mono);
  }
  .result-md :global(pre) {
    margin: 8px 0;
    padding: 12px 14px;
    overflow-x: auto;
    border: 0;
    border-radius: 6px;
    background: var(--code-bg);
    font-size: 12.5px;
    line-height: 1.55;
  }
  .result-md :global(pre code) {
    padding: 0;
    background: none;
  }
  .result-md :global(a) {
    color: var(--info);
  }
  .result-md :global(blockquote) {
    margin: 8px 0;
    padding: 4px 12px;
    border-left: 1px solid var(--line-strong);
    color: var(--muted);
  }
  .result-md :global(table) {
    margin: 8px 0;
    border-collapse: collapse;
  }
  .result-md :global(th),
  .result-md :global(td) {
    padding: 4px 8px;
    border: 1px solid var(--line);
    text-align: left;
  }
  .turn-error {
    margin: 8px 0 0;
    padding: 12px 14px;
    border: 1px solid var(--err-line);
    border-radius: 6px;
    color: var(--err);
    background: var(--err-bg);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    font-size: 12.5px;
    line-height: 1.55;
  }
  .turn-degraded {
    margin: 8px 0 0;
    padding: 8px 12px;
    border: 1px solid var(--attn);
    border-radius: var(--radius-lg);
    color: var(--attn-text);
    background: var(--attn-soft);
    font-size: var(--size-meta);
    line-height: 1.5;
  }
  .badge-failed {
    color: var(--err);
    font-weight: 600;
  }
  .empty {
    padding: 8px 4px;
    color: var(--muted);
    font-size: var(--size-detail);
  }
  .transcript-empty {
    padding: 32px 0;
  }
  @media (prefers-reduced-motion: no-preference) {
    .live-dot {
      animation: pulse 1.2s ease-in-out infinite;
    }
  }
  @keyframes pulse {
    50% {
      opacity: 0.35;
    }
  }
  @media (max-width: 760px) {
    .turns {
      padding-right: 16px;
      padding-left: 16px;
    }
    .steps summary {
      min-height: 44px;
    }
  }
</style>
