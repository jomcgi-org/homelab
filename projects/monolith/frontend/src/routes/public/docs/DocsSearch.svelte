<script>
  import { goto } from "$app/navigation";

  /**
   * Client-side navigational search over the doc titles already shipped in the
   * sidebar (no bodies cross to the browser). Filters project, kind, label,
   * and title; full-text content search remains a separate concern.
   * @type {{ docs: {slug:string,title:string,project:string,kind:string,label:string}[] }}
   */
  let { docs } = $props();

  let query = $state("");
  let open = $state(false);
  let activeIndex = $state(0);
  /** @type {HTMLInputElement | undefined} */
  let inputEl = $state();
  /** @type {HTMLElement | undefined} */
  let rootEl = $state();

  const results = $derived.by(() => {
    const q = query.trim().toLowerCase();
    if (!q) return [];
    const out = [];
    for (const d of docs) {
      if (
        `${d.project} ${d.kind} ${d.label} ${d.title}`.toLowerCase().includes(q)
      )
        out.push(d);
      if (out.length >= 12) break;
    }
    return out;
  });

  // Reset the keyboard highlight whenever the result set changes.
  $effect(() => {
    void results;
    activeIndex = 0;
  });

  function close() {
    open = false;
    query = "";
  }

  /** @param {{slug:string}} d */
  function pick(d) {
    close();
    inputEl?.blur();
    goto(`/docs/${d.slug}`);
  }

  /** @param {KeyboardEvent} e */
  function onKeydown(e) {
    if (e.key === "Escape") {
      close();
      inputEl?.blur();
      return;
    }
    if (!results.length) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      activeIndex = (activeIndex + 1) % results.length;
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      activeIndex = (activeIndex - 1 + results.length) % results.length;
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (results[activeIndex]) pick(results[activeIndex]);
    }
  }

  // Cmd/Ctrl+K focuses the box, matching the old VitePress search shortcut.
  /** @param {KeyboardEvent} e */
  function onWindowKeydown(e) {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      inputEl?.focus();
    }
  }

  /** @param {MouseEvent} e */
  function onWindowClick(e) {
    if (open && rootEl && !rootEl.contains(/** @type {Node} */ (e.target)))
      close();
  }
</script>

<svelte:window onkeydown={onWindowKeydown} onclick={onWindowClick} />

<div class="docs-search" bind:this={rootEl}>
  <span class="docs-search-icon" aria-hidden="true">
    <svg width="14" height="14" viewBox="0 0 24 24">
      <circle
        cx="11"
        cy="11"
        r="7"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
      />
      <path
        d="M21 21 L16 16"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        stroke-linecap="round"
      />
    </svg>
  </span>
  <input
    bind:this={inputEl}
    bind:value={query}
    type="search"
    class="docs-search-input"
    placeholder="Search docs"
    aria-label="Search documentation"
    autocomplete="off"
    onfocus={() => (open = true)}
    onkeydown={onKeydown}
  />
  <kbd class="docs-search-kbd" aria-hidden="true">⌘K</kbd>

  {#if open && results.length}
    <ul class="docs-search-results" role="listbox">
      {#each results as d, i}
        <li role="option" aria-selected={i === activeIndex}>
          <a
            href={`/docs/${d.slug}`}
            class="docs-search-item"
            class:active={i === activeIndex}
            onmouseenter={() => (activeIndex = i)}
            onclick={(e) => {
              e.preventDefault();
              pick(d);
            }}
          >
            <span class="docs-search-title">{d.title}</span>
            <span class="docs-search-context">{d.project} · {d.label}</span>
          </a>
        </li>
      {/each}
    </ul>
  {:else if open && query.trim()}
    <div class="docs-search-empty">No matches</div>
  {/if}
</div>

<style>
  .docs-search {
    position: relative;
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px 10px;
    background: var(--paper);
    border: 2px solid var(--ink);
    box-shadow: var(--shadow-hard-sm);
  }

  .docs-search-icon {
    display: grid;
    place-items: center;
    color: var(--ink-3);
    flex: 0 0 auto;
  }

  .docs-search-input {
    border: none;
    outline: none;
    background: transparent;
    font-family: var(--mono);
    font-size: 12px;
    letter-spacing: 0.04em;
    color: var(--ink);
    width: 150px;
    padding: 0;
  }

  .docs-search-input::placeholder {
    color: var(--ink-3);
  }

  /* Strip the native search clear affordance for a consistent brutalist look. */
  .docs-search-input::-webkit-search-decoration,
  .docs-search-input::-webkit-search-cancel-button {
    appearance: none;
  }

  .docs-search-kbd {
    font-family: var(--mono);
    font-size: 10px;
    font-weight: 600;
    color: var(--ink-3);
    border: 1px solid var(--rule-2);
    border-radius: 3px;
    padding: 1px 5px;
    flex: 0 0 auto;
  }

  /* ── Dropdown ── */
  .docs-search-results {
    position: absolute;
    top: calc(100% + 6px);
    right: 0;
    left: 0;
    z-index: 60;
    list-style: none;
    margin: 0;
    padding: 6px;
    max-height: min(60vh, 420px);
    overflow-y: auto;
    background: var(--paper);
    border: 2px solid var(--ink);
    box-shadow: var(--shadow-hard);
  }

  .docs-search-item {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 12px;
    padding: 8px 10px;
    text-decoration: none;
    border: 2px solid transparent;
    transition:
      background 120ms ease,
      border-color 120ms ease;
  }

  .docs-search-item.active {
    border-color: var(--ink);
    background: var(--accent);
  }

  .docs-search-title {
    font-family: var(--sans);
    font-size: 13px;
    font-weight: 600;
    color: var(--ink);
    line-height: 1.25;
  }

  .docs-search-context {
    flex: 0 0 auto;
    font-family: var(--mono);
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--ink-3);
  }

  .docs-search-empty {
    position: absolute;
    top: calc(100% + 6px);
    right: 0;
    left: 0;
    z-index: 60;
    padding: 12px;
    text-align: center;
    font-family: var(--mono);
    font-size: 11px;
    color: var(--ink-3);
    background: var(--paper);
    border: 2px solid var(--ink);
    box-shadow: var(--shadow-hard);
  }

  @media (max-width: 720px) {
    .docs-search-kbd {
      display: none;
    }
    .docs-search-input {
      width: 96px;
    }
  }
</style>
