<script>
  import { RUN_LEXICON as P } from "./run-lexicon.js";

  let {
    card,
    sessionId,
    onSend = async () => {},
    onAnswered = () => {},
  } = $props();

  let sending = $state(false);

  async function choose(option) {
    if (sending || card.answered || sessionId == null) return;
    sending = true;
    try {
      await onSend({ session_id: sessionId, prompt: option });
      onAnswered(card.key);
    } finally {
      sending = false;
    }
  }
</script>

<div class="ask-body">
  <div class="question">
    <p>{card.question || card.ref}</p>
    <span>{card.ref}</span>
  </div>
  <div class="options">
    {#each card.options ?? [] as option, index (`${option}:${index}`)}
      <button
        class:primary={index === 0}
        type="button"
        disabled={sending || card.answered || sessionId == null}
        onclick={() => choose(option)}
      >
        <span>{option}</span>
        <span class="say">{P.labels.sayOption.replace("{option}", option)}</span
        >
      </button>
    {/each}
  </div>
</div>

<style>
  .ask-body {
    display: flex;
    gap: 32px;
    padding: 16px;
  }
  .question {
    min-width: 0;
    flex: 1;
  }
  .question p {
    margin: 0;
    color: var(--text);
    font-size: 15px;
    line-height: 1.5;
  }
  .question span {
    display: block;
    margin-top: 8px;
    color: var(--muted);
    font: var(--size-meta) var(--font-mono);
  }
  .options {
    width: 220px;
    flex: 0 0 220px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  button {
    width: 100%;
    min-height: 40px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 0 12px;
    border: 1px solid var(--line-strong);
    border-radius: var(--radius-md);
    color: var(--text);
    background: var(--panel-bg);
    font: 500 13px var(--font-ui);
    text-align: left;
  }
  button.primary {
    border-color: var(--ink);
    color: var(--ink-text);
    background: var(--ink);
  }
  button:focus-visible {
    outline: 2px solid var(--info);
    outline-offset: 2px;
  }
  button:disabled {
    cursor: not-allowed;
    opacity: 0.6;
  }
  .say {
    font: var(--size-meta) var(--font-mono);
    white-space: nowrap;
    opacity: 0.72;
  }
  @media (max-width: 760px) {
    .ask-body {
      flex-direction: column;
      gap: 16px;
    }
    .options {
      width: 100%;
      flex-basis: auto;
    }
    button {
      min-height: 48px;
    }
  }
</style>
