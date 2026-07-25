<script>
  import { fade } from "svelte/transition";

  /** @type {{ target?: string, visible?: boolean }} */
  let { target = "#homelab", visible = true } = $props();

  function handleClick() {
    const el = document.querySelector(target);
    if (el) el.scrollIntoView({ behavior: "smooth" });
  }
</script>

{#if visible}
  <button
    class="scroll-bar"
    onclick={handleClick}
    transition:fade={{ duration: 300 }}
  >
    <span class="scroll-bar-inner">
      <span class="scroll-bar-label">HOMELAB SLOS</span>
      <span class="scroll-bar-arrow">↓</span>
    </span>
  </button>
{/if}

<style>
  .scroll-bar {
    display: flex;
    align-items: flex-end;
    justify-content: center;
    width: 100%;
    padding: 14px 0;
    background: var(--cream);
    border: none;
    border-top: 1px solid var(--ink);
    border-bottom: 2px solid var(--ink);
    color: var(--ink);
    font-family: var(--mono);
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.12em;
    cursor: pointer;
    border-radius: 0;
    -webkit-appearance: none;
    flex: 1;
    transition: background 160ms ease;
  }

  .scroll-bar:hover {
    background: var(--paper);
  }

  .scroll-bar-inner {
    display: flex;
    align-items: center;
    gap: 10px;
  }

  .scroll-bar-arrow {
    font-size: 14px;
    line-height: 1;
    animation: nudge 2s infinite ease-in-out;
  }

  @keyframes nudge {
    0%,
    100% {
      transform: translateY(0);
    }
    50% {
      transform: translateY(3px);
    }
  }

  .scroll-bar:hover .scroll-bar-arrow {
    animation: none;
  }
</style>
