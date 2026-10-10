<script>
  import { tick } from "svelte";
  import RevealEditor from "./RevealEditor.svelte";
  let { endpoint, characters, changed } = $props();
  let open = $state(false);
  let opener = $state(null);
  let panel = $state(null);

  async function show() {
    open = true;
    await tick();
    panel?.focus();
  }

  async function close() {
    open = false;
    await tick();
    opener?.focus();
  }

  function keydown(event) {
    if (event.key === "Escape") {
      event.stopPropagation();
      close();
    }
  }
</script>

<button
  bind:this={opener}
  class="opener"
  aria-haspopup="dialog"
  aria-expanded={open}
  onclick={show}>Reveal knowledge</button
>
{#if open}
  <div
    bind:this={panel}
    class="grimoire drawer"
    role="dialog"
    aria-labelledby="reveal-panel-title"
    tabindex="-1"
    onkeydown={keydown}
  >
    <div class="head">
      <h2 id="reveal-panel-title">Reveal to your players</h2>
      <button onclick={close}>Close reveal panel</button>
    </div>
    <RevealEditor {endpoint} {characters} {changed} />
  </div>
{/if}

<style>
  .opener {
    font: inherit;
    padding: 10px 14px;
    color: var(--grim-ink);
    background: var(--grim-surface);
    border: 1px solid var(--grim-line);
    cursor: pointer;
  }
  .drawer {
    position: fixed;
    right: 0;
    top: 0;
    bottom: 0;
    width: min(440px, 100%);
    padding: 24px;
    box-sizing: border-box;
    overflow-y: auto;
    background: var(--grim-surface);
    color: var(--grim-ink);
    border-left: 1px solid var(--grim-line);
    box-shadow: -12px 0 40px #0002;
    z-index: 20;
  }
  .head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }
  .head h2 {
    margin: 0;
  }
  .head button {
    font: inherit;
    padding: 10px;
    color: var(--grim-ink);
    background: var(--grim-surface);
    border: 1px solid var(--grim-line);
    cursor: pointer;
  }
</style>
