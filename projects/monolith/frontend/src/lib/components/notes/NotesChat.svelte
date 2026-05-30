<script>
  /**
   * Collapsible RAG chat panel for the notes knowledge graph.
   *
   * Props:
   *   apiBase  – backend chat endpoint prefix (e.g. "/api/knowledge/public")
   *   isPublic – true for the public page (shows rate-limit badge and notice)
   */
  let { apiBase = "/api/knowledge", isPublic = false } = $props();

  // ── state ───────────────────────────────────────────────────────────
  let open = $state(false);
  let question = $state("");
  let messages = $state(/** @type {Array<{role:string,text:string,error?:boolean}>} */ ([]));
  let streaming = $state(false);
  let inputRef = $state(null);

  // Rate-limit tracking (public endpoint exposes headers).
  let rlRemaining = $state(null); // null = unknown
  let rlLimit = $state(null);
  let rlReset = $state(null); // epoch seconds
  let rateLimited = $state(false);
  let retryIn = $state(0);

  $effect(() => {
    if (open && inputRef) inputRef.focus();
  });

  function toggle() {
    open = !open;
  }

  function formatReset(epoch) {
    if (!epoch) return "";
    const diff = Math.max(0, Math.ceil(epoch - Date.now() / 1000));
    if (diff === 0) return "now";
    return `${diff}s`;
  }

  function remainingLabel() {
    if (!isPublic || rlLimit === null) return null;
    return `${rlRemaining ?? "?"}/${rlLimit} req/min`;
  }

  async function submit(e) {
    e.preventDefault();
    const q = question.trim();
    if (!q || streaming) return;

    question = "";
    messages = [...messages, { role: "user", text: q }];
    streaming = true;
    rateLimited = false;

    const assistantIdx = messages.length;
    messages = [...messages, { role: "assistant", text: "" }];

    try {
      const res = await fetch(`${apiBase}/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: q }),
      });

      // Parse rate-limit headers (present on both 200 and 429).
      if (isPublic) {
        const limit = res.headers.get("X-RateLimit-Limit");
        const remaining = res.headers.get("X-RateLimit-Remaining");
        const reset = res.headers.get("X-RateLimit-Reset");
        if (limit) rlLimit = parseInt(limit);
        if (remaining) rlRemaining = parseInt(remaining);
        if (reset) rlReset = parseInt(reset);
      }

      if (res.status === 429) {
        const body = await res.json();
        rateLimited = true;
        retryIn = body.retry_after ?? 60;
        messages = messages.map((m, i) =>
          i === assistantIdx
            ? { role: "assistant", text: body.message ?? "Rate limited.", error: true }
            : m,
        );
        streaming = false;
        return;
      }

      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`);
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const lines = buf.split("\n");
        buf = lines.pop() ?? "";
        for (const line of lines) {
          if (!line.startsWith("data:")) continue;
          const raw = line.slice(5).trim();
          if (!raw) continue;
          try {
            const evt = JSON.parse(raw);
            if (evt.type === "text_chunk") {
              messages = messages.map((m, i) =>
                i === assistantIdx ? { ...m, text: m.text + evt.text } : m,
              );
            } else if (evt.type === "error") {
              messages = messages.map((m, i) =>
                i === assistantIdx
                  ? { ...m, text: evt.message ?? "Error from inference.", error: true }
                  : m,
              );
            }
          } catch {
            // malformed chunk — ignore
          }
        }
      }
    } catch (err) {
      messages = messages.map((m, i) =>
        i === assistantIdx ? { ...m, text: "Connection error.", error: true } : m,
      );
    } finally {
      streaming = false;
    }
  }

  function onKeydown(e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit(e);
    }
  }
</script>

<!-- ── Toggle button ─────────────────────────────────────────────── -->
<button
  class="chat-toggle"
  class:open
  onclick={toggle}
  aria-label={open ? "Close notes chat" : "Ask the notes"}
  aria-expanded={open}
>
  <span class="toggle-label">
    {open ? "✕ CLOSE" : "ASK THE NOTES"}
  </span>
  {#if isPublic && rlLimit !== null && !open}
    <span class="rl-badge">{rlRemaining ?? rlLimit}/{rlLimit}</span>
  {/if}
</button>

<!-- ── Chat panel ────────────────────────────────────────────────── -->
{#if open}
  <div class="chat-panel" role="complementary" aria-label="Notes chat">
    <div class="chat-header">
      <span class="eyebrow">ASK THE NOTES</span>
      {#if isPublic}
        <span class="rl-notice" title="Running on Joe's in-cluster GPU — rate limited to {rlLimit ?? 5} req/min to prevent abuse">
          ⚡ in-cluster · {rlLimit ?? 5} req/min
          {#if rlRemaining !== null}
            · {rlRemaining} left
            {#if rlReset}· resets {formatReset(rlReset)}{/if}
          {/if}
        </span>
      {/if}
    </div>

    <div class="chat-messages" aria-live="polite">
      {#if messages.length === 0}
        <p class="chat-empty">Ask anything about these notes.</p>
      {/if}
      {#each messages as msg}
        <div class="msg msg-{msg.role}" class:error={msg.error}>
          <span class="msg-role">{msg.role === "user" ? "YOU" : "NOTES"}</span>
          <span class="msg-text">{msg.text}</span>
          {#if msg.role === "assistant" && streaming && !msg.text}
            <span class="cursor">▋</span>
          {/if}
        </div>
      {/each}
    </div>

    {#if rateLimited}
      <div class="rate-limit-banner">
        ⏸ Rate limited — this runs on Joe's homelab GPU.
        Retry in {retryIn}s.
        <span class="rl-explain">
          Intentionally throttled at {rlLimit ?? 5} req/min to prevent abuse on shared in-cluster infra.
        </span>
      </div>
    {/if}

    <form class="chat-input-row" onsubmit={submit}>
      <textarea
        bind:this={inputRef}
        bind:value={question}
        onkeydown={onKeydown}
        placeholder="Ask a question…"
        rows="2"
        disabled={streaming}
        aria-label="Your question"
      ></textarea>
      <button type="submit" class="send-btn" disabled={streaming || !question.trim()}>
        {streaming ? "…" : "→"}
      </button>
    </form>
  </div>
{/if}

<style>
  /* ── Toggle button ────────────────────────────────────────────── */
  .chat-toggle {
    position: absolute;
    bottom: 20px;
    right: 20px;
    z-index: 10;
    display: flex;
    align-items: center;
    gap: 8px;
    background: #ffde01;
    border: 1.5px solid #141414;
    box-shadow: 4px 4px 0 #141414;
    font-family: "JetBrains Mono", ui-monospace, "SF Mono", monospace;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: #141414;
    padding: 8px 14px;
    cursor: pointer;
    transition:
      transform 100ms ease,
      box-shadow 100ms ease;
  }

  .chat-toggle:hover {
    transform: translate(-2px, -2px);
    box-shadow: 6px 6px 0 #141414;
  }

  .chat-toggle:active {
    transform: none;
    box-shadow: 2px 2px 0 #141414;
  }

  .chat-toggle.open {
    background: #f1ebdc;
  }

  .rl-badge {
    background: #141414;
    color: #ffde01;
    font-size: 9px;
    padding: 1px 5px;
    letter-spacing: 0.08em;
  }

  /* ── Chat panel ──────────────────────────────────────────────── */
  .chat-panel {
    position: absolute;
    bottom: 64px;
    right: 20px;
    z-index: 10;
    width: 380px;
    max-width: calc(100vw - 40px);
    background: #ffffff;
    border: 1.5px solid #141414;
    box-shadow: 6px 6px 0 #141414;
    display: flex;
    flex-direction: column;
    font-family: "JetBrains Mono", ui-monospace, "SF Mono", monospace;
    font-size: 12px;
    color: #141414;
    max-height: 60vh;
  }

  .chat-header {
    display: flex;
    align-items: baseline;
    gap: 10px;
    flex-wrap: wrap;
    padding: 8px 12px 6px;
    border-bottom: 1.5px solid #141414;
    background: #f1ebdc;
  }

  .eyebrow {
    font-size: 9px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: #6b6658;
    flex-shrink: 0;
  }

  .rl-notice {
    font-size: 9px;
    letter-spacing: 0.06em;
    color: #6b6658;
    flex: 1;
    text-align: right;
    cursor: help;
  }

  /* ── Messages ─────────────────────────────────────────────────── */
  .chat-messages {
    flex: 1;
    overflow-y: auto;
    padding: 10px 12px;
    display: flex;
    flex-direction: column;
    gap: 10px;
    scrollbar-width: thin;
    scrollbar-color: #ddd5c3 transparent;
  }

  .chat-empty {
    color: #8a857a;
    font-size: 11px;
    letter-spacing: 0.04em;
    text-align: center;
    margin-top: 12px;
  }

  .msg {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .msg-role {
    font-size: 8px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: #8a857a;
  }

  .msg-user .msg-role {
    color: #141414;
  }

  .msg-text {
    white-space: pre-wrap;
    word-break: break-word;
    line-height: 1.5;
    font-size: 12px;
  }

  .msg.error .msg-text {
    color: #ff7169;
  }

  .cursor {
    display: inline-block;
    animation: blink 900ms step-end infinite;
  }

  @keyframes blink {
    50% { opacity: 0; }
  }

  /* ── Rate-limit banner ─────────────────────────────────────────── */
  .rate-limit-banner {
    padding: 8px 12px;
    background: #fff3cd;
    border-top: 1.5px solid #141414;
    font-size: 10px;
    line-height: 1.5;
    color: #141414;
  }

  .rl-explain {
    display: block;
    color: #6b6658;
    margin-top: 2px;
    font-size: 9px;
    letter-spacing: 0.04em;
  }

  /* ── Input row ─────────────────────────────────────────────────── */
  .chat-input-row {
    display: flex;
    gap: 0;
    border-top: 1.5px solid #141414;
  }

  textarea {
    flex: 1;
    border: none;
    outline: none;
    resize: none;
    padding: 8px 10px;
    font-family: inherit;
    font-size: 12px;
    background: #f8f6f0;
    color: #141414;
    caret-color: #ff7169;
    line-height: 1.5;
  }

  textarea::placeholder {
    color: rgba(20, 20, 20, 0.32);
  }

  textarea:disabled {
    opacity: 0.6;
  }

  .send-btn {
    width: 40px;
    border: none;
    border-left: 1.5px solid #141414;
    background: #141414;
    color: #ffde01;
    font-family: inherit;
    font-size: 16px;
    cursor: pointer;
    transition: background 100ms ease;
    flex-shrink: 0;
  }

  .send-btn:hover:not(:disabled) {
    background: #2a2824;
  }

  .send-btn:disabled {
    background: #8a857a;
    cursor: not-allowed;
  }
</style>
