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

<div class="search" bind:this={rootEl}>
  <p class="sec-label">/ Search</p>
  <div class="field">
    <input
      bind:this={inputEl}
      bind:value={query}
      type="search"
      class="input"
      placeholder="Search docs"
      aria-label="Search documentation"
      autocomplete="off"
      onfocus={() => (open = true)}
      onkeydown={onKeydown}
    />
    <kbd aria-hidden="true">⌘K</kbd>
  </div>

  {#if open && results.length}
    <ul class="results" role="listbox">
      {#each results as d, i (d.slug)}
        <li role="option" aria-selected={i === activeIndex}>
          <a
            href={`/docs/${d.slug}`}
            class="result"
            class:active={i === activeIndex}
            onmouseenter={() => (activeIndex = i)}
            onclick={(e) => {
              e.preventDefault();
              pick(d);
            }}
          >
            <span class="title">{d.title}</span>
            <span class="context">{d.project} / {d.label}</span>
          </a>
        </li>
      {/each}
    </ul>
  {:else if open && query.trim()}
    <p class="empty">No matches</p>
  {/if}
</div>

<style>
  .search {
    position: relative;
    margin-bottom: 1.8rem;
  }

  /* One outlined box partitioned by a rule: the input, then the shortcut
     hint as its own cell. */
  .field {
    display: flex;
    align-items: stretch;
    border: 1px solid var(--stroke);
    background: var(--sheet);
  }

  .input {
    flex: 1;
    min-width: 0;
    padding: 0.45em 0.6em;
    border: 0;
    background: transparent;
    color: var(--ink);
    font-family: var(--font-code);
    font-size: 0.75rem;
  }

  .input::placeholder {
    color: var(--ink-2);
  }

  .input::-webkit-search-decoration,
  .input::-webkit-search-cancel-button {
    appearance: none;
  }

  .input:focus-visible {
    outline: 2px solid var(--accent-ink);
    outline-offset: -2px;
  }

  kbd {
    display: flex;
    align-items: center;
    padding: 0 0.6em;
    border-left: 1px solid var(--stroke);
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.62rem;
  }

  /* The results drop over the page as one more outlined panel on the sheet;
     the active row is marked the way the spine marks a selection. */
  .results,
  .empty {
    position: absolute;
    top: 100%;
    right: 0;
    left: 0;
    z-index: 60;
    margin: -1px 0 0;
    padding: 0;
    border: 1px solid var(--ink);
    background: var(--sheet);
    list-style: none;
  }

  .results {
    max-height: min(60vh, 420px);
    overflow-y: auto;
  }

  .results li + li {
    border-top: 1px solid var(--line);
  }

  .result {
    display: flex;
    flex-direction: column;
    gap: 0.15em;
    padding: 0.5em 0.6em 0.5em 0.5rem;
    border-left: 2px solid transparent;
    text-decoration: none;
  }

  .result.active {
    border-left-color: var(--accent-ink);
    background: var(--band);
  }

  .title {
    color: var(--ink);
    font-size: 0.8rem;
    font-weight: 600;
    line-height: 1.25;
  }

  .result.active .title {
    color: var(--accent-ink);
  }

  .context {
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.62rem;
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }

  .empty {
    padding: 0.6em;
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.72rem;
  }

  @media (max-width: 760px) {
    kbd {
      display: none;
    }
  }
</style>
