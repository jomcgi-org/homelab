<script>
  import { onMount } from "svelte";
  import "$lib/private/dashboard-theme.css";
  import { relativeTime } from "../run-history.js";
  import {
    EFFECT_WORD,
    HOTKEYS,
    chatBody,
    confirmLine,
    decisionBody,
    effectLine,
    escapeForKey,
    escapeHotkey,
    moveCursor,
    needsConfirm,
    open as openOnes,
    optionForKey,
    resolutionLine,
    resolved as resolvedOnes,
  } from "./escalations-view.js";

  let { data } = $props();

  const POLL_MS = 20000;

  let dark = $state(
    typeof window !== "undefined" &&
      window.matchMedia("(prefers-color-scheme: dark)").matches,
  );

  // svelte-ignore state_referenced_locally
  let escalations = $state(data.escalations ?? []);
  // svelte-ignore state_referenced_locally
  let unavailable = $state(data.error);
  let cursor = $state(0);
  // Notes are per receipt, never one box shared by the page. A single note
  // state sent whatever was typed on one card along with a decision clicked
  // on another, so a scope note written about one issue could land as the
  // comment on a different one.
  let notes = $state({});
  let busy = $state(null);
  let failure = $state(null);
  let notice = $state(null);
  let now = $state(Date.now());
  let noteBox = $state(null);
  // The receipt whose close is armed, or null. Closing is the one escape
  // another button cannot undo, so it is asked about once before it is sent.
  let confirming = $state(null);

  const pending = $derived(openOnes(escalations));
  const settled = $derived(resolvedOnes(escalations));
  // The card a keypress acts on. An armed close pins it to the card that was
  // armed rather than to the cursor: a poll can reorder the list under an
  // armed confirmation, and the second press must never land on a different
  // issue from the first.
  const current = $derived(
    pending.find((row) => row.receipt_id === confirming) ??
      pending[cursor] ??
      null,
  );

  async function refresh() {
    try {
      const response = await fetch("/agents/escalations");
      if (!response.ok) throw new Error("escalations unavailable");
      const body = await response.json();
      escalations = body.escalations ?? [];
      unavailable = false;
      now = Date.now();
      // An armed close whose card the refresh took away is disarmed, so the
      // arming never outlives the issue it was about.
      if (
        confirming !== null &&
        !openOnes(escalations).some((row) => row.receipt_id === confirming)
      ) {
        confirming = null;
      }
    } catch {
      unavailable = true;
    }
  }

  function noteFor(item) {
    return notes[item?.receipt_id] ?? "";
  }

  function setNote(item, value) {
    notes = { ...notes, [item.receipt_id]: value };
  }

  async function send(item, body, label) {
    if (!item || busy) return;
    busy = label;
    failure = null;
    notice = null;
    try {
      const response = await fetch(
        `/agents/escalations/decisions/${item.receipt_id}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        },
      );
      const result = await response.json().catch(() => ({}));
      if (!response.ok) {
        failure = result.detail ?? `the decision failed (${response.status})`;
        return;
      }
      // A chat the lane could not take still posted the question, so the
      // difference between "asked and scheduled" and "asked and nothing will
      // answer" has to reach the operator rather than reading as success.
      if (result.requeued === false) {
        notice = `Asked on #${item.issue_number}, but no brief was queued: ${
          result.blocked_by ?? "the lane would not admit it"
        }`;
      }
      const { [item.receipt_id]: _spent, ...rest } = notes;
      notes = rest;
      // The list is re-read rather than patched in place: the effect happened
      // on GitHub and the receipt is the record of it, so what the server says
      // now is the only honest thing to render.
      await refresh();
      cursor = Math.min(cursor, Math.max(openOnes(escalations).length - 1, 0));
    } catch {
      failure = "the decision could not be sent";
    } finally {
      busy = null;
    }
  }

  /**
   * Put the cursor on the card that was acted on. A mouse click used to leave
   * the cursor where it was, so arming a close on one card and then pressing
   * a key acted on another card entirely.
   */
  function focusOn(item) {
    const index = pending.findIndex(
      (row) => row.receipt_id === item.receipt_id,
    );
    if (index >= 0) cursor = index;
  }

  function decide(item, option) {
    focusOn(item);
    if (item.briefing) {
      failure = "a brief is running on this issue; decide when it settles";
      return;
    }
    confirming = null;
    return send(item, decisionBody(option.key, noteFor(item)), option.key);
  }

  /**
   * The fixed way out, on every card whatever the brief offered. The close
   * arms on the first press and sends on the second; the other two send at
   * once, because a defer is reversible and a dismiss writes nothing.
   */
  function escapeWith(item, option) {
    focusOn(item);
    if (item.briefing) {
      failure = "a brief is running on this issue; decide when it settles";
      return;
    }
    if (needsConfirm(option) && confirming !== item.receipt_id) {
      confirming = item.receipt_id;
      failure = null;
      notice = null;
      return;
    }
    confirming = null;
    return send(item, decisionBody(option.key, noteFor(item)), option.key);
  }

  function armed(item, option) {
    return needsConfirm(option) && confirming === item.receipt_id;
  }

  function chat(item) {
    if (!noteFor(item).trim()) {
      failure = "a chat request needs a note saying what is missing";
      noteBox?.focus();
      return;
    }
    return send(item, chatBody(noteFor(item)), "chat");
  }

  function typing(event) {
    const tag = event.target?.tagName;
    return (
      tag === "INPUT" || tag === "TEXTAREA" || event.target?.isContentEditable
    );
  }

  function onKey(event) {
    if (event.metaKey || event.ctrlKey || event.altKey || typing(event)) return;
    if (event.key === "j" || event.key === "k") {
      cursor = moveCursor(cursor, event.key === "j" ? 1 : -1, pending.length);
      confirming = null;
      event.preventDefault();
      return;
    }
    if (event.key === "c") {
      noteBox?.focus();
      event.preventDefault();
      return;
    }
    // Escape cancels an armed close before it dismisses anything, so the key
    // that gets you out of the confirmation is the one already under your
    // finger rather than a second one to learn.
    if (event.key === "Escape" && confirming !== null) {
      confirming = null;
      event.preventDefault();
      return;
    }
    const way = escapeForKey(current, event.key);
    if (way) {
      escapeWith(current, way);
      event.preventDefault();
      return;
    }
    const option = optionForKey(current, event.key);
    if (option) {
      decide(current, option);
      event.preventDefault();
    }
  }

  onMount(() => {
    const scheme = window.matchMedia("(prefers-color-scheme: dark)");
    const apply = () => (dark = scheme.matches);
    apply();
    scheme.addEventListener("change", apply);
    const timer = setInterval(() => {
      if (document.visibilityState === "visible" && !busy) refresh();
    }, POLL_MS);
    const tick = setInterval(() => (now = Date.now()), 30000);
    return () => {
      scheme.removeEventListener("change", apply);
      clearInterval(timer);
      clearInterval(tick);
    };
  });
</script>

<svelte:head>
  <title>Escalations</title>
</svelte:head>

<svelte:window on:keydown={onKey} />

<main class="escalations-page shell {dark ? 'night' : 'day'}">
  <div class="frame">
    <h1 class="sr-only">Factory escalations</h1>

    <header class="masthead">
      <nav class="view-tabs" aria-label="Agents views">
        <a class="here" href="/agents/escalations" aria-current="page"
          >escalations</a
        >
        <a href="/agents/factory">factory</a>
        <a href="/agents">sessions</a>
        <a href="/agents/drain">knowledge extraction queue</a>
      </nav>
    </header>

    <section class="stats" aria-label="Escalation state">
      <div>
        <span class="k">open</span>
        <span class="v num">{pending.length}</span>
      </div>
      <div>
        <span class="k">decided</span>
        <span class="v num">{settled.length}</span>
      </div>
      <div>
        <span class="k">keys</span>
        <span class="v keys">j k 1-4 c x d esc</span>
      </div>
    </section>

    {#if unavailable}
      <p class="warn-line">
        The board is unavailable. Retrying every 20 seconds.
      </p>
    {/if}
    {#if failure}
      <p class="warn-line" role="alert">{failure}</p>
    {/if}
    {#if notice}
      <p class="warn-line" role="status">{notice}</p>
    {/if}

    <section aria-label="Open escalations">
      <p class="sec-label">/ Waiting on you</p>
      {#if !pending.length}
        <p class="none">
          Nothing is waiting. The lane decides the rest itself.
        </p>
      {/if}
      {#each pending as item, index (item.receipt_id)}
        <article class="panel" class:here={index === cursor}>
          <header class="panel-head">
            <span class="issue code">#{item.issue_number}</span>
            <span class="title">{item.title}</span>
            <span class="badge">{item.task_class}</span>
            <span class="badge">recommend {item.recommendation}</span>
            {#if item.briefing}
              <span class="badge briefing">briefing</span>
            {/if}
          </header>

          <div class="body">
            {#if item.summary}
              <p class="outcome">{item.summary}</p>
            {/if}
            <p class="question">{item.question}</p>
            {#each item.chat as asked, i (i)}
              <p class="asked code">
                asked {relativeTime(asked.asked_at, now)}: {asked.note}
                {#if asked.requeued === false}
                  <span class="unqueued"
                    >no brief queued: {asked.blocked_by ??
                      "the lane would not admit it"}</span
                  >
                {/if}
              </p>
            {/each}

            <div class="options">
              {#each item.options as option, i (option.key)}
                <button
                  class="option"
                  class:primary={i === 0}
                  disabled={busy !== null || item.briefing}
                  onclick={() => decide(item, option)}
                >
                  <span class="hotkey code">{HOTKEYS[i] ?? ""}</span>
                  <span class="label">{option.label}</span>
                  <span class="effect code"
                    >{EFFECT_WORD[option.effect] ?? option.effect} ·
                    {effectLine(option)}</span
                  >
                </button>
              {/each}
            </div>

            {#if item.escape?.length}
              <div class="escapes">
                <span class="escapes-label code">escape</span>
                {#each item.escape as way (way.key)}
                  <button
                    class="escape-btn"
                    class:armed={armed(item, way)}
                    disabled={busy !== null || item.briefing}
                    onclick={() => escapeWith(item, way)}
                  >
                    <span class="hotkey code">{escapeHotkey(way)}</span>
                    <span class="label"
                      >{armed(item, way) ? "Confirm close" : way.label}</span
                    >
                    <span class="effect code">{effectLine(way)}</span>
                  </button>
                {/each}
              </div>
              {#if confirming === item.receipt_id}
                <p class="confirm code" role="status">{confirmLine(item)}</p>
              {/if}
            {/if}

            {#if index === cursor}
              <div class="chat">
                <label class="sr-only" for={`note-${item.receipt_id}`}>
                  Note for issue {item.issue_number}
                </label>
                <textarea
                  id={`note-${item.receipt_id}`}
                  bind:this={noteBox}
                  value={noteFor(item)}
                  oninput={(event) => setNote(item, event.target.value)}
                  rows="2"
                  placeholder="What is missing, or a note to record with the decision"
                ></textarea>
                <button
                  class="option chat-button"
                  disabled={busy !== null}
                  onclick={() => chat(item)}
                >
                  <span class="hotkey code">c</span>
                  <span class="label">Needs more chat</span>
                  <span class="effect code"
                    >asks on the issue and re-briefs it</span
                  >
                </button>
              </div>
            {/if}

            <p class="links code">
              <a href={item.url}>issue on GitHub</a>
              {#if item.comment_url}
                <a href={item.comment_url}>the brief</a>
              {/if}
              {#if item.downgraded}
                <span class="badge">close refused, escalated</span>
              {/if}
            </p>
          </div>
        </article>
      {/each}
    </section>

    {#if settled.length}
      <section aria-label="Decided escalations">
        <details>
          <summary class="sec-label">/ Decided ({settled.length})</summary>
          <ol class="ledger code">
            {#each settled as item (item.receipt_id)}
              <li>
                <a href={item.url}>#{item.issue_number}</a>
                <span class="clip">{item.title}</span>
                <span>{resolutionLine(item)}</span>
                <span>{relativeTime(item.resolved?.decided_at, now)}</span>
              </li>
            {/each}
          </ol>
        </details>
      </section>
    {/if}
  </div>
</main>

<style>
  :global(body) {
    margin: 0;
  }

  /* One bright sheet with a fixed root basis, the same pairing the factory
     board and the updates page use. */
  :global(html:has(.escalations-page)) {
    background: #ffffff; /* nosemgrep: svelte-hardcoded-color-in-style */
    font-size: 16px;
  }
  @media (prefers-color-scheme: dark) {
    :global(html:has(.escalations-page)) {
      background: #181a20; /* nosemgrep: svelte-hardcoded-color-in-style */
    }
  }

  .escalations-page {
    --band: color-mix(in srgb, var(--ink) 5%, var(--sheet));
    --accent-ink: color-mix(in srgb, var(--accent) 85%, var(--ink));
    --stroke: color-mix(in srgb, var(--ink) 28%, transparent);
    min-height: 100vh;
    box-sizing: border-box;
    padding: clamp(3.25rem, 5vh, 3.75rem) clamp(1rem, 4vw, 4.5em) 6em;
    color: var(--ink);
    background: var(--sheet);
    font-family: var(--font-ui);
    font-size: 16px;
    line-height: 1.45;
  }
  .frame {
    max-width: 56em;
    margin: 0 auto;
  }
  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    margin: -1px;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
  }
  .code {
    font-family: var(--font-code);
  }
  .num {
    font-variant-numeric: tabular-nums;
  }
  a {
    color: var(--accent-ink);
  }
  a:focus-visible,
  button:focus-visible,
  textarea:focus-visible {
    outline: 2px solid var(--accent-ink);
    outline-offset: 3px;
  }

  .masthead {
    display: flex;
    flex-wrap: wrap;
    gap: 0.75rem;
    margin-bottom: 1rem;
  }
  .view-tabs {
    display: flex;
    flex-wrap: wrap;
    border: 1px solid var(--stroke);
    font-family: var(--font-code);
    font-size: 0.72rem;
    letter-spacing: 0.04em;
  }
  .view-tabs > a {
    display: inline-flex;
    align-items: center;
    min-height: 2.75rem;
    padding: 0 0.9em;
    border-bottom: 2px solid transparent;
    color: var(--ink-2);
    text-decoration: none;
  }
  .view-tabs > a + a {
    border-left: 1px solid var(--stroke);
  }
  .view-tabs a:hover {
    color: var(--ink);
  }
  .view-tabs .here {
    border-bottom-color: var(--accent-ink);
    color: var(--accent-ink);
  }

  .stats {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    border: 1px solid var(--ink);
  }
  .stats > div {
    min-width: 0;
    padding: 0.55rem 0.8rem 0.6rem;
    border-right: 1px solid var(--stroke);
  }
  .stats > div:last-child {
    border-right: 0;
  }
  .stats .k {
    display: block;
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.66rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }
  .stats .v {
    display: block;
    margin-top: 0.3em;
    overflow: hidden;
    font-size: 1.25rem;
    font-weight: 700;
    letter-spacing: -0.02em;
    line-height: 1.1;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .stats .v.keys {
    font-family: var(--font-code);
    font-size: 0.9rem;
    font-weight: 500;
  }

  .warn-line {
    margin: 0.75rem 0 0;
    color: var(--warn);
    font-family: var(--font-code);
    font-size: 0.75rem;
  }

  section {
    margin-top: 2.25rem;
  }
  .sec-label {
    margin: 0 0 0.8em;
    padding-bottom: 0.5em;
    border-bottom: 1px solid var(--stroke);
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.68rem;
    font-weight: 500;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }
  summary.sec-label {
    cursor: pointer;
  }
  .none {
    margin: 0;
    color: var(--ink-2);
    font-size: 0.9rem;
  }

  .panel {
    margin-bottom: 0.9rem;
    border: 1px solid var(--ink);
  }
  /* The cursor marker, the tier's 2px left bar on a vertical list. Drawn
     inside the border so the panel does not shift as it moves. */
  .panel.here {
    box-shadow: inset 3px 0 0 0 var(--accent-ink);
  }
  .panel-head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.4rem 0.8rem;
    min-height: 2.75rem;
    padding: 0.55rem 0.8rem;
    border-bottom: 1px solid var(--stroke);
    background: var(--band);
  }
  .issue {
    color: var(--ink-2);
    font-size: 0.78rem;
  }
  .title {
    flex: 1 1 14em;
    min-width: 0;
    font-size: 0.95rem;
    font-weight: 700;
    letter-spacing: -0.01em;
  }
  .badge {
    padding: 0.1em 0.55em;
    border: 1px solid var(--stroke);
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.66rem;
    letter-spacing: 0.06em;
  }

  .body {
    padding: 0.8rem;
  }
  .outcome {
    margin: 0 0 0.6em;
    font-size: 0.92rem;
  }
  .question {
    margin: 0 0 0.9em;
    font-size: 0.95rem;
    font-weight: 600;
  }
  .asked {
    margin: 0 0 0.6em;
    color: var(--ink-2);
    font-size: 0.74rem;
  }
  .unqueued {
    color: var(--warn);
  }
  .badge.briefing {
    border-color: var(--warn);
    color: var(--warn);
  }

  .options {
    display: grid;
    gap: 0.5rem;
  }
  .option {
    display: grid;
    grid-template-columns: 1.6rem minmax(0, 1fr);
    gap: 0.15rem 0.6rem;
    min-height: 2.75rem;
    padding: 0.5rem 0.7rem;
    border: 1px solid var(--stroke);
    background: var(--sheet);
    color: var(--ink);
    font: inherit;
    text-align: left;
    cursor: pointer;
  }
  .option:hover:not(:disabled) {
    background: var(--band);
  }
  .option:disabled {
    cursor: default;
    opacity: 0.55;
  }
  /* The recommendation is the one primary control on the panel: a 1px ink
     outline against the hairline the alternatives wear. No fill, no radius. */
  .option.primary {
    border: 1px solid var(--ink);
  }
  .option .hotkey {
    grid-row: 1 / span 2;
    align-self: center;
    color: var(--ink-2);
    font-size: 0.8rem;
    text-align: center;
  }
  .option .label {
    font-size: 0.92rem;
    font-weight: 600;
  }
  .option.primary .label {
    color: var(--accent-ink);
  }
  .option .effect {
    color: var(--ink-2);
    font-size: 0.7rem;
  }

  /* The escape row. Always the same three, always quieter than the brief's
     own options, so they read as a way out rather than as a fifth answer:
     hairline border, --ink-2 label, no primary outline. */
  .escapes {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.4rem 0.5rem;
    margin-top: 0.7rem;
    padding-top: 0.7rem;
    border-top: 1px solid var(--line);
  }
  .escapes-label {
    color: var(--ink-2);
    font-size: 0.66rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }
  .escape-btn {
    display: inline-grid;
    grid-template-columns: 1.4rem minmax(0, 1fr);
    gap: 0.1rem 0.5rem;
    align-items: center;
    min-height: 2.75rem;
    padding: 0.35rem 0.6rem;
    border: 1px solid var(--line);
    background: var(--sheet);
    color: var(--ink-2);
    font: inherit;
    text-align: left;
    cursor: pointer;
  }
  .escape-btn:hover:not(:disabled) {
    border-color: var(--stroke);
    background: var(--band);
    color: var(--ink);
  }
  .escape-btn:disabled {
    cursor: default;
    opacity: 0.55;
  }
  .escape-btn.armed {
    border-color: var(--warn);
    color: var(--warn);
  }
  .escape-btn .hotkey {
    grid-row: 1 / span 2;
    align-self: center;
    font-size: 0.78rem;
    text-align: center;
  }
  .escape-btn .label {
    color: inherit;
    font-size: 0.84rem;
    font-weight: 600;
  }
  .escape-btn .effect {
    color: var(--ink-2);
    font-size: 0.66rem;
  }
  .confirm {
    margin: 0.6rem 0 0;
    color: var(--warn);
    font-size: 0.72rem;
  }

  .chat {
    display: grid;
    gap: 0.5rem;
    margin-top: 0.9rem;
    padding-top: 0.9rem;
    border-top: 1px solid var(--stroke);
  }
  .chat textarea {
    box-sizing: border-box;
    width: 100%;
    padding: 0.5rem 0.6rem;
    border: 1px solid var(--stroke);
    background: var(--sheet);
    color: var(--ink);
    font-family: var(--font-ui);
    font-size: 0.9rem;
    resize: vertical;
  }

  .links {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem 1rem;
    margin: 0.9rem 0 0;
    font-size: 0.74rem;
  }

  .ledger {
    margin: 0;
    padding: 0;
    list-style: none;
    font-size: 0.76rem;
  }
  .ledger li {
    display: grid;
    grid-template-columns: 5em minmax(0, 1fr) minmax(0, 1fr) 6em;
    gap: 0.6rem;
    padding: 0.45rem 0;
    border-bottom: 1px solid var(--line);
    color: var(--ink-2);
  }
  .clip {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  @media (max-width: 40em) {
    .ledger li {
      grid-template-columns: 5em minmax(0, 1fr);
    }
  }
</style>
