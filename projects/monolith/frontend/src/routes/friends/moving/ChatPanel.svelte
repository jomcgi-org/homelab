<script>
  import { onMount } from "svelte";
  import {
    CHARACTER_LIMIT,
    applyFrame,
    initialTurnState,
    streamChatMessage,
  } from "./chat-stream.js";

  let transcript = $state([]);
  let message = $state("");
  let busy = $state(false);
  let error = $state("");
  let completionAnnouncement = $state("");
  let panel = $state();
  let messages = $state();
  let controller;
  let nextId = 0;

  $effect(() => {
    if (messages && transcript.some((item) => item.content)) {
      messages.scrollTop = messages.scrollHeight;
    }
  });

  onMount(() => {
    const dialog = panel?.closest("dialog");
    const abort = () => controller?.abort();
    dialog?.addEventListener("close", abort);
    return () => {
      dialog?.removeEventListener("close", abort);
      controller?.abort();
    };
  });

  async function send(event) {
    event.preventDefault();
    const content = message.trim();
    if (!content) return;

    controller?.abort();
    controller = new AbortController();
    const activeController = controller;
    const history = transcript
      .filter((item) => item.content)
      .map(({ role, content: turnContent }) => ({
        role,
        content: turnContent,
      }))
      .slice(-12);
    const userTurn = { id: ++nextId, role: "user", content };
    const assistantId = ++nextId;
    transcript = [
      ...transcript,
      userTurn,
      { id: assistantId, role: "assistant", content: "" },
    ];
    message = "";
    error = "";
    completionAnnouncement = "";
    busy = true;
    let turn = initialTurnState();

    try {
      await streamChatMessage(content, history, {
        signal: activeController.signal,
        onFrame: (frame) => {
          turn = applyFrame(turn, frame);
          transcript = transcript.map((item) =>
            item.id === assistantId
              ? { ...item, content: turn.assistant }
              : item,
          );
          if (turn.error) error = turn.error;
          if (turn.status === "done") {
            completionAnnouncement = "Assistant response complete.";
            busy = false;
          } else if (turn.status === "error") {
            busy = false;
          }
        },
      });
    } catch (caught) {
      if (caught?.name === "AbortError") {
        transcript = transcript.filter(
          (item) => item.id !== assistantId || item.content,
        );
      } else {
        error = "Something went wrong. Please try again.";
      }
    } finally {
      if (controller === activeController) {
        controller = undefined;
        busy = false;
      }
    }
  }
</script>

<section class="chat" bind:this={panel} aria-label="Moving plan chat">
  <div class="chat-messages" bind:this={messages}>
    {#if transcript.length === 0}
      <p class="chat-empty">Ask about dates, tasks, roles, or collisions.</p>
    {/if}
    {#each transcript as turn (turn.id)}
      {#if turn.content}
        <p class:viewer={turn.role === "user"} class="chat-message">
          {turn.content}
        </p>
      {/if}
    {/each}
  </div>
  <span class="chat-status" role="status">{completionAnnouncement}</span>
  {#if error}
    <p class="chat-error" role="alert">{error}</p>
  {/if}
  <form class="chat-form" onsubmit={send}>
    <label class="sr-only" for="moving-chat-message">Ask about the plan</label>
    <input
      id="moving-chat-message"
      name="message"
      type="text"
      maxlength={CHARACTER_LIMIT}
      placeholder="Ask about the plan"
      autocomplete="off"
      disabled={busy}
      bind:value={message}
    />
    <button type="submit" disabled={busy || !message.trim()}>
      {busy ? "Thinking" : "Send"}
    </button>
  </form>
  <p class="chat-hint">Conversations are not saved.</p>
</section>
