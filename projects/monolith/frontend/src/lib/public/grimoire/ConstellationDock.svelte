<script>
  // Fixed bottom-right dock for the cross-page "trail of your curiosity":
  // every entity touched anywhere in the Grimoire app (chat grounding, World
  // card opens, reader mention taps) accrues into the shared
  // constellationStore (constellation-store.js), and this dock is the one
  // place that surfaces it on every page except chat (chat keeps its own
  // large session panel; see +layout.svelte for the route check that skips
  // mounting this component there).
  //
  // Collapsed state is a small pill (node count + a MiniConstellation
  // preview); click toggles an expanded panel listing every node as a
  // type-colored link into World, focused on that entity. Escape closes.
  // Hidden entirely while the constellation is empty so it never occupies
  // space with nothing to show.
  import { onDestroy } from "svelte";
  import { constellationStore } from "./constellation-store.js";
  import { worldHref } from "./api.js";
  import MiniConstellation from "./MiniConstellation.svelte";

  let state = $state({ nodes: [], ids: new Set(), edges: [] });
  const unsubscribe = constellationStore.subscribe((s) => {
    state = s;
  });
  onDestroy(unsubscribe);

  let expanded = $state(false);

  const REDUCED_MOTION =
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  const TYPE_ALLOWLIST = /^[a-z_]+$/;

  function typeVar(entityType) {
    const type = TYPE_ALLOWLIST.test(entityType ?? "") ? entityType : "class";
    return `var(--grim-type-${type}, currentColor)`;
  }

  function toggle() {
    expanded = !expanded;
  }

  function close() {
    expanded = false;
  }

  function onKeydown(e) {
    if (expanded && e.key === "Escape") {
      e.preventDefault();
      close();
    }
  }
</script>

<svelte:window onkeydown={onKeydown} />

{#if state.nodes.length > 0}
  <div class="constel-dock" class:reduced={REDUCED_MOTION}>
    {#if expanded}
      <div
        class="dock-panel"
        role="dialog"
        aria-label="People and places you've explored"
      >
        <div class="dock-panel-head">
          <span class="dock-panel-title">YOUR TRAIL</span>
          <button
            type="button"
            class="dock-close"
            onclick={close}
            aria-label="Close"
          >
            &times;
          </button>
        </div>
        <ul class="dock-list">
          {#each state.nodes as n (n.id)}
            <li>
              <a
                class="dock-item"
                href={worldHref(n.id)}
                style="--dock-item-color: {typeVar(n.entity_type)}"
              >
                <span class="dock-dot"></span>
                <span class="dock-name">{n.name || "untitled"}</span>
              </a>
            </li>
          {/each}
        </ul>
      </div>
    {/if}

    <button
      type="button"
      class="dock-pill"
      onclick={toggle}
      aria-expanded={expanded}
      aria-label={`${state.nodes.length} people and places in your trail, ${expanded ? "collapse" : "expand"}`}
    >
      <span class="dock-preview">
        <MiniConstellation
          nodes={state.nodes}
          edges={state.edges}
          revealedIds={new Set(state.ids)}
        />
      </span>
      <span class="dock-count">{state.nodes.length}</span>
    </button>
  </div>
{/if}

<style>
  .constel-dock {
    position: fixed;
    right: 20px;
    bottom: 20px;
    z-index: 40;
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: 10px;
  }

  .dock-pill {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px 12px 6px 6px;
    border: 1px solid var(--grim-line);
    border-radius: 999px;
    background: var(--grim-surface);
    color: var(--grim-ink);
    cursor: pointer;
    box-shadow: 0 4px 14px rgba(0, 0, 0, 0.14);
    transition:
      transform 160ms ease,
      border-color 120ms ease;
  }
  .dock-pill:hover,
  .dock-pill:focus-visible {
    border-color: var(--grim-accent);
    transform: translateY(-1px);
  }
  .constel-dock.reduced .dock-pill:hover {
    transform: none;
  }

  .dock-preview {
    width: 34px;
    height: 34px;
    border-radius: 50%;
    overflow: hidden;
    display: block;
    flex: none;
    background: var(--grim-paper);
  }

  .dock-count {
    font-family: var(--font-mono);
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.04em;
    color: var(--grim-text-dim);
  }

  .dock-panel {
    width: 230px;
    max-height: min(360px, 60vh);
    display: flex;
    flex-direction: column;
    border: 1px solid var(--grim-line);
    border-radius: 10px;
    background: var(--grim-surface);
    box-shadow: 0 10px 30px rgba(0, 0, 0, 0.18);
    overflow: hidden;
    transform-origin: bottom right;
    animation: dock-panel-in 180ms cubic-bezier(0.22, 1, 0.36, 1);
  }
  .constel-dock.reduced .dock-panel {
    animation: none;
  }
  @keyframes dock-panel-in {
    from {
      opacity: 0;
      transform: scale(0.92) translateY(6px);
    }
    to {
      opacity: 1;
      transform: scale(1) translateY(0);
    }
  }

  .dock-panel-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 8px 10px 14px;
    border-bottom: 1px solid var(--grim-line);
  }
  .dock-panel-title {
    font-family: var(--font-mono);
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.14em;
    color: var(--grim-text-faint);
  }
  .dock-close {
    font-size: 16px;
    line-height: 1;
    padding: 4px 8px;
    border: none;
    background: none;
    color: var(--grim-text-dim);
    cursor: pointer;
    border-radius: 6px;
  }
  .dock-close:hover {
    background: var(--grim-accent-soft);
    color: var(--grim-ink);
  }

  .dock-list {
    list-style: none;
    margin: 0;
    padding: 6px;
    overflow-y: auto;
  }

  .dock-item {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 7px 8px;
    border-radius: 6px;
    text-decoration: none;
    color: var(--grim-ink);
    font-size: 12.5px;
    transition: background 120ms ease;
  }
  .dock-item:hover,
  .dock-item:focus-visible {
    background: var(--grim-accent-soft);
  }
  .dock-dot {
    width: 8px;
    height: 8px;
    border-radius: 999px;
    background: var(--dock-item-color);
    flex: none;
  }
  .dock-name {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  @media (max-width: 640px) {
    .constel-dock {
      right: 12px;
      bottom: 12px;
    }
    .dock-panel {
      width: min(230px, calc(100vw - 24px));
    }
  }
</style>
