<script>
  // Streaming chat panel for the cluster agent (POST /api/chat/cluster via
  // the /dashboard-chat SSE proxy). Self-contained: message history lives in
  // component state, capped to the last 12 turns when sent upstream.
  const HISTORY_CAP = 12;

  let messages = $state([]);
  let input = $state("");
  let streaming = $state(false);
  let logEl = $state(null);

  function scrollToBottom() {
    if (!logEl) return;
    requestAnimationFrame(() => {
      logEl.scrollTop = logEl.scrollHeight;
    });
  }

  function formatArgs(args) {
    if (!args || typeof args !== "object") return "";
    return Object.values(args)
      .filter((v) => v !== null && v !== undefined && v !== "")
      .map((v) => (typeof v === "object" ? JSON.stringify(v) : String(v)))
      .join(" ");
  }

  async function send() {
    const text = input.trim();
    if (!text || streaming) return;
    input = "";

    // History is the prior turns with actual text, capped to the last 12.
    const history = messages
      .filter((m) => m.content)
      .map((m) => ({ role: m.role, content: m.content }))
      .slice(-HISTORY_CAP);

    messages.push({ role: "user", content: text, tools: [], error: "" });
    messages.push({ role: "assistant", content: "", tools: [], error: "" });
    const current = messages[messages.length - 1];
    streaming = true;
    scrollToBottom();

    try {
      const res = await fetch("/dashboard-chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, history }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);

      // Same reader/parse loop as the knowledge explorer page.
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n\n");
        buffer = lines.pop() || "";

        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          try {
            const event = JSON.parse(line.slice(6));
            if (event.type === "text_chunk") {
              current.content += event.data.text;
              scrollToBottom();
            } else if (event.type === "tool_call") {
              current.tools.push(
                `> ${event.data.tool} ${formatArgs(event.data.args)}`.trim(),
              );
              scrollToBottom();
            } else if (event.type === "error") {
              current.error = event.data.message;
            } else if (event.type === "done") {
              // Stream complete
            }
          } catch {
            // Skip malformed events
          }
        }
      }
    } catch (err) {
      current.error = err.message;
    } finally {
      streaming = false;
      scrollToBottom();
    }
  }

  function onKeydown(e) {
    if (e.key === "Enter") {
      e.preventDefault();
      send();
    }
  }
</script>

<div class="cluster-chat">
  <div class="chat-log" bind:this={logEl}>
    {#if messages.length === 0}
      <p class="chat-empty">
        ask about pods, deploys, events, logs... the agent reads the cluster
        live
      </p>
    {/if}
    {#each messages as msg, i}
      {#if msg.role === "user"}
        <div class="msg msg--user">{msg.content}</div>
      {:else}
        <div class="msg msg--assistant">
          {#each msg.tools as tool}
            <div class="tool-line">{tool}</div>
          {/each}
          {#if msg.content}
            <div class="msg-text">
              {msg.content}{#if i === messages.length - 1 && streaming}<span
                  class="cursor">|</span
                >{/if}
            </div>
          {:else if i === messages.length - 1 && streaming}
            <div class="msg-text"><span class="cursor">|</span></div>
          {/if}
          {#if msg.error}
            <div class="error-line">{msg.error}</div>
          {/if}
        </div>
      {/if}
    {/each}
  </div>
  <div class="input-bar">
    <input
      type="text"
      bind:value={input}
      onkeydown={onKeydown}
      placeholder={streaming ? "thinking..." : "ask the cluster..."}
      disabled={streaming}
      aria-label="Ask the cluster"
    />
    <button onclick={send} disabled={streaming || !input.trim()}>
      &rarr;
    </button>
  </div>
</div>

<style>
  .cluster-chat {
    display: flex;
    flex-direction: column;
    /* Full-width panel at the foot of the dashboard: the log gets the room so
     * the transcript dominates and the input is a slim rail beneath it. */
    min-height: 300px;
    max-height: 560px;
    font-family: var(--font);
  }

  .chat-log {
    flex: 1 1 0;
    overflow-y: auto;
    min-height: 0;
    display: flex;
    flex-direction: column;
    gap: 10px;
    padding: 4px 0 12px 0;
    scrollbar-width: thin;
    scrollbar-color: var(--fg-tertiary) transparent;
  }

  .chat-empty {
    font-size: 13px;
    color: var(--fg-tertiary);
    margin: 0;
  }

  .msg {
    font-size: 13px;
    line-height: 1.5;
    /* Cap the measure on the now-wide panel so lines stay readable instead of
     * running the full width; still 90% on narrow screens. */
    max-width: min(90%, 720px);
  }

  .msg--user {
    align-self: flex-end;
    text-align: right;
    color: var(--fg-secondary);
    white-space: pre-wrap;
  }

  .msg--assistant {
    align-self: flex-start;
    border-left: 1px solid var(--border);
    padding-left: 12px;
  }

  .msg-text {
    color: var(--fg);
    white-space: pre-wrap;
    word-break: break-word;
  }

  .tool-line {
    font-size: 12px;
    color: var(--fg-tertiary);
    font-variant-numeric: tabular-nums;
    padding: 2px 0;
    word-break: break-all;
  }

  .error-line {
    font-size: 13px;
    color: var(--danger);
    margin-top: 4px;
  }

  .cursor {
    animation: blink 0.8s step-end infinite;
    color: var(--fg-secondary);
  }

  @keyframes blink {
    50% {
      opacity: 0;
    }
  }

  .input-bar {
    display: flex;
    border-top: 1px solid var(--border);
  }

  .input-bar input {
    flex: 1;
    font-family: var(--font);
    font-size: 13px;
    color: var(--fg);
    background: transparent;
    border: none;
    outline: none;
    padding: 10px 0;
  }

  .input-bar input::placeholder {
    color: var(--fg-tertiary);
  }

  .input-bar input:disabled {
    opacity: 0.5;
  }

  .input-bar button {
    font-family: var(--font);
    font-size: 14px;
    font-weight: 700;
    color: var(--fg);
    background: transparent;
    border: none;
    padding: 10px 0 10px 12px;
    cursor: pointer;
  }

  .input-bar button:disabled {
    opacity: 0.3;
    cursor: default;
  }
</style>
