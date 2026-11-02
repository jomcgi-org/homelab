<script>
  // One factory decision, answerable in place on a phone. The question leads,
  // each answer is a full-width button saying what it does (the
  // recommendation first and outlined), and the ways out sit behind one
  // disclosure. Sending follows the escalations page's protocol through
  // decision-client.js; this component only draws and reports.
  import {
    chatBody,
    decisionBody,
    effectLine,
    needsConfirm,
  } from "../../../routes/private/factory/escalations/escalations-view.js";
  import { sendDecision } from "../decision-client.js";

  let { item, ondecided = () => {} } = $props();

  let busy = $state(null);
  let message = $state("");
  let tone = $state("");
  let note = $state("");
  let armed = $state(false);

  async function send(body, label) {
    if (busy) return;
    if (item.briefing) {
      tone = "warn";
      message = "A brief is running on this issue. Answer when it settles.";
      return;
    }
    busy = label;
    message = "";
    try {
      const out = await sendDecision(item, body, {
        fetchImpl: (...args) => fetch(...args),
        storage: sessionStorage,
      });
      tone = out.status === "accepted" ? "ok" : "warn";
      message = out.message;
      if (out.status === "accepted") ondecided(item, out);
    } catch {
      tone = "warn";
      message =
        "Outcome unknown. Check the escalations page before answering again.";
    } finally {
      busy = null;
      armed = false;
    }
  }

  function answer(option) {
    return send(decisionBody(option.key, note, item.decision_id), option.key);
  }

  function escape(option) {
    if (needsConfirm(option) && !armed) {
      armed = true;
      tone = "warn";
      message = `Tap "${option.label}" again to close #${item.issue_number} as not planned.`;
      return;
    }
    return send(decisionBody(option.key, note, item.decision_id), option.key);
  }

  function ask() {
    if (!note.trim()) {
      tone = "warn";
      message = "Say what is missing first.";
      return;
    }
    return send(chatBody(note, item.decision_id), "chat");
  }
</script>

<article class="decision" aria-labelledby="decision-{item.receipt_id}">
  <p class="ref">
    <a
      href={item.url ?? "/factory/escalations"}
      target={item.url ? "_blank" : undefined}
      rel="noopener">#{item.issue_number}</a
    >
    {#if item.work_item_id}<a href="/factory/work-items/{item.work_item_id}"
        >work item {item.work_item_id}</a
      >{/if}
    {#if item.pr_url}<a href={item.pr_url} target="_blank" rel="noopener"
        >pull request</a
      >{/if}
  </p>
  <h3 id="decision-{item.receipt_id}" class="title">{item.title}</h3>
  {#if item.question}<p class="question">{item.question}</p>{/if}
  {#if item.summary}
    <details class="why">
      <summary>Why it stopped</summary>
      <p>{item.summary}</p>
    </details>
  {/if}

  <div class="answers">
    {#each item.options as option, i (option.key)}
      <button
        type="button"
        class="answer"
        class:recommended={i === 0}
        disabled={busy !== null}
        onclick={() => answer(option)}
      >
        <span class="label"
          >{option.label}{#if i === 0}<span class="rec">recommended</span
            >{/if}</span
        >
        <span class="effect">{effectLine(option, item.kind)}</span>
        {#if busy === option.key}<span class="sending">sending</span>{/if}
      </button>
    {/each}
  </div>

  {#if message}
    <p class="message {tone}" role={tone === "warn" ? "alert" : "status"}>
      {message}
      {#if tone === "warn"}<a href={item.url ?? "/factory/escalations"}
          >Open in escalations</a
        >{/if}
    </p>
  {/if}

  <details class="more">
    <summary>Other ways out</summary>
    <label class="note">
      <span>Note (sent with any answer)</span>
      <textarea bind:value={note} rows="2" placeholder="What is missing, or why"
      ></textarea>
    </label>
    <div class="answers">
      <button
        type="button"
        class="answer quiet"
        disabled={busy !== null}
        onclick={ask}
      >
        <span class="label">Ask for more</span>
        <span class="effect">asks on the issue and re-briefs it</span>
      </button>
      {#each item.escape as option (option.key)}
        <button
          type="button"
          class="answer quiet"
          class:armed={armed && needsConfirm(option)}
          disabled={busy !== null}
          onclick={() => escape(option)}
        >
          <span class="label">{option.label}</span>
          <span class="effect">{effectLine(option, item.kind)}</span>
        </button>
      {/each}
    </div>
  </details>
</article>

<style>
  .decision {
    padding: 0.9rem 1rem 1rem;
  }
  .decision + :global(.decision) {
    border-top: 1px solid var(--stroke);
  }
  .ref {
    display: flex;
    flex-wrap: wrap;
    gap: 0 1rem;
    margin: 0;
    font: 0.75rem var(--font-code);
  }
  .ref a {
    display: inline-flex;
    align-items: center;
    min-height: 44px;
    color: var(--ink-2);
    text-decoration: none;
  }
  .ref a:hover {
    color: var(--accent-ink);
  }
  .title {
    margin: 0;
    font-size: 0.95rem;
    font-weight: 600;
    line-height: 1.35;
    overflow-wrap: anywhere;
  }
  .question {
    margin: 0.5rem 0 0;
    font-size: 1.05rem;
    font-weight: 650;
    line-height: 1.35;
  }
  .why {
    margin-top: 0.4rem;
    color: var(--ink-2);
    font-size: 0.88rem;
  }
  .why p {
    margin: 0.3rem 0 0;
  }
  summary {
    display: flex;
    align-items: center;
    min-height: 44px;
    color: var(--ink-2);
    font: 0.78rem var(--font-code);
    cursor: pointer;
    list-style: none;
  }
  summary::-webkit-details-marker {
    display: none;
  }
  summary::after {
    margin-left: auto;
    content: "+";
  }
  details[open] > summary::after {
    content: "\2212";
  }
  .answers {
    display: grid;
    gap: 0.5rem;
    margin-top: 0.75rem;
  }
  .answer {
    display: grid;
    gap: 0.15rem;
    width: 100%;
    min-height: 56px;
    padding: 0.65rem 0.85rem;
    border: 1px solid var(--stroke);
    border-radius: 0;
    background: var(--sheet);
    color: var(--ink);
    font: inherit;
    text-align: left;
    cursor: pointer;
    touch-action: manipulation;
  }
  .answer:active:not(:disabled) {
    background: var(--band);
  }
  .answer.recommended {
    border: 2px solid var(--ink);
  }
  .answer.quiet {
    min-height: 48px;
    border-color: var(--line);
    color: var(--ink-2);
  }
  .answer.armed {
    border-color: var(--warn);
    color: var(--warn);
  }
  .answer:disabled {
    opacity: 0.55;
    cursor: default;
  }
  .label {
    display: flex;
    align-items: baseline;
    gap: 0.6rem;
    font-size: 0.98rem;
    font-weight: 600;
  }
  .rec {
    color: var(--ok);
    font: 500 0.7rem var(--font-code);
  }
  .effect {
    color: var(--ink-2);
    font-size: 0.82rem;
    line-height: 1.35;
  }
  .sending {
    color: var(--ink-2);
    font: 0.72rem var(--font-code);
  }
  .message {
    margin: 0.6rem 0 0;
    font-size: 0.88rem;
  }
  .message.ok {
    color: var(--ok);
  }
  .message.warn {
    color: var(--warn);
  }
  .message a {
    display: inline-flex;
    align-items: center;
    min-height: 44px;
    margin-left: 0.4rem;
    color: var(--ink);
  }
  .more {
    margin-top: 0.4rem;
  }
  .note {
    display: grid;
    gap: 0.3rem;
    color: var(--ink-2);
    font: 0.75rem var(--font-code);
  }
  .note textarea {
    width: 100%;
    padding: 0.5rem 0.6rem;
    border: 1px solid var(--stroke);
    border-radius: 0;
    background: var(--sheet);
    color: var(--ink);
    font: 1rem var(--font-ui);
    resize: vertical;
  }
  .decision :focus-visible {
    outline: 2px solid var(--accent-ink);
    outline-offset: 2px;
  }
</style>
