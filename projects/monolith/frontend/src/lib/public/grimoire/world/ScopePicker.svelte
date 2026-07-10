<script>
  // World scope picker: an ARIA combobox replacing the old native <select>.
  // "Everything" first, then every adventure grouped under its book_display_name
  // heading, with a filter input to narrow a long list. Reports the chosen
  // scope value ("everything" | "adventure:{id}") up via `onchange`. Keyboard:
  // arrows move the active option, Enter selects, Escape closes; the button
  // shows the current label.
  let {
    adventures = [],
    value = "everything",
    disabled = false,
    onchange = null,
  } = $props();

  let open = $state(false);
  let filter = $state("");
  let activeIdx = $state(0);

  let rootEl;
  let buttonEl;
  let filterEl;

  // Flat option list in display order: "Everything" first, then adventures in
  // the order given (already book/seq-ordered by listAllAdventures). Each row
  // carries its scope value, label, and the book heading it belongs under (null
  // for Everything). Filtered by the case-insensitive substring in `filter`.
  const options = $derived.by(() => {
    const all = [
      { value: "everything", label: "Everything", book: null },
      ...adventures.map((a) => ({
        value: `adventure:${a.id}`,
        label: a.name,
        book: a.book_display_name ?? "",
      })),
    ];
    const f = filter.trim().toLowerCase();
    if (!f) return all;
    return all.filter(
      (o) =>
        o.label.toLowerCase().includes(f) ||
        (o.book ?? "").toLowerCase().includes(f),
    );
  });

  // Group the filtered options for rendering, preserving order. Everything sits
  // in its own leading, heading-less group.
  const groups = $derived.by(() => {
    const out = [];
    let cur = null;
    for (const o of options) {
      const head = o.book;
      if (!cur || cur.head !== head) {
        cur = { head, rows: [] };
        out.push(cur);
      }
      cur.rows.push(o);
    }
    return out;
  });

  const currentLabel = $derived(
    options.find((o) => o.value === value)?.label ??
      adventures.find((a) => `adventure:${a.id}` === value)?.name ??
      "Everything",
  );

  function openMenu() {
    if (disabled) return;
    open = true;
    filter = "";
    const idx = options.findIndex((o) => o.value === value);
    activeIdx = idx >= 0 ? idx : 0;
    queueMicrotask(() => filterEl?.focus());
  }

  function closeMenu(refocus = true) {
    open = false;
    if (refocus) buttonEl?.focus();
  }

  function choose(opt) {
    if (!opt) return;
    open = false;
    buttonEl?.focus();
    if (opt.value !== value) onchange?.(opt.value);
  }

  function onFilterInput() {
    // Keep the active option in range as the list shrinks.
    activeIdx = options.length ? Math.min(activeIdx, options.length - 1) : 0;
  }

  function onKeydown(e) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (options.length) activeIdx = (activeIdx + 1) % options.length;
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      if (options.length)
        activeIdx = (activeIdx - 1 + options.length) % options.length;
    } else if (e.key === "Enter") {
      e.preventDefault();
      choose(options[activeIdx]);
    } else if (e.key === "Escape") {
      e.preventDefault();
      closeMenu();
    } else if (e.key === "Home") {
      e.preventDefault();
      activeIdx = 0;
    } else if (e.key === "End") {
      e.preventDefault();
      activeIdx = Math.max(0, options.length - 1);
    }
  }

  function onFocusOut(e) {
    if (rootEl && !rootEl.contains(e.relatedTarget)) open = false;
  }

  function optionId(o) {
    return `scope-opt-${o.value}`;
  }
</script>

<div class="scope-picker" bind:this={rootEl} onfocusout={onFocusOut}>
  <span class="eyebrow">Scope</span>
  <button
    bind:this={buttonEl}
    type="button"
    class="scope-button"
    {disabled}
    role="combobox"
    aria-haspopup="listbox"
    aria-expanded={open}
    aria-controls="scope-listbox"
    aria-label={`Scope: ${currentLabel}`}
    onclick={() => (open ? closeMenu() : openMenu())}
  >
    <span class="scope-label">{currentLabel}</span>
    <span class="chevron" aria-hidden="true">▾</span>
  </button>

  {#if open}
    <div class="scope-pop">
      <input
        bind:this={filterEl}
        class="scope-filter"
        type="text"
        placeholder="Filter adventures..."
        autocomplete="off"
        aria-label="Filter scope options"
        aria-controls="scope-listbox"
        aria-activedescendant={options[activeIdx]
          ? optionId(options[activeIdx])
          : null}
        bind:value={filter}
        oninput={onFilterInput}
        onkeydown={onKeydown}
      />
      <ul
        class="scope-list"
        role="listbox"
        id="scope-listbox"
        aria-label="Scope"
      >
        {#if options.length === 0}
          <li class="scope-empty" role="presentation">No adventures match.</li>
        {/if}
        {#each groups as g (g.head ?? "__everything")}
          {#if g.head}
            <li class="scope-group-head" role="presentation">{g.head}</li>
          {/if}
          {#each g.rows as o (o.value)}
            {@const idx = options.indexOf(o)}
            <li
              class="scope-option"
              class:active={idx === activeIdx}
              class:selected={o.value === value}
              id={optionId(o)}
              role="option"
              aria-selected={o.value === value}
              onmousedown={(e) => {
                e.preventDefault();
                choose(o);
              }}
              onmousemove={() => (activeIdx = idx)}
            >
              {o.label}
            </li>
          {/each}
        {/each}
      </ul>
    </div>
  {/if}
</div>

<style>
  .scope-picker {
    position: relative;
    display: inline-flex;
    align-items: center;
    gap: 8px;
  }

  .eyebrow {
    font-size: 11px;
    letter-spacing: 0.22em;
    text-transform: uppercase;
    color: var(--grim-text-faint);
    font-weight: 600;
  }

  .scope-button {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    font-family: var(--grim-serif);
    font-size: 15px;
    font-weight: 600;
    color: var(--grim-ink);
    background: var(--grim-surface);
    border: 1px solid var(--grim-line);
    border-radius: 8px;
    padding: 8px 12px;
    cursor: pointer;
    max-width: 46vw;
  }

  .scope-button:disabled {
    opacity: 0.6;
    cursor: default;
  }

  .scope-button:focus-visible {
    border-color: var(--grim-accent);
    box-shadow: 0 0 0 3px var(--grim-accent-soft);
    outline: none;
  }

  .scope-label {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .chevron {
    color: var(--grim-text-faint);
    font-size: 11px;
    flex: none;
  }

  .scope-pop {
    position: absolute;
    top: calc(100% + 6px);
    left: 0;
    z-index: 30;
    width: min(320px, 80vw);
    background: var(--grim-surface);
    border: 1px solid var(--grim-line);
    border-radius: 10px;
    box-shadow: 0 12px 32px rgba(20, 30, 50, 0.16);
    padding: 6px;
  }

  .scope-filter {
    width: 100%;
    height: 34px;
    padding: 0 10px;
    margin-bottom: 4px;
    font-size: 13px;
    font-family: inherit;
    color: var(--grim-ink);
    background: var(--grim-surface-2);
    border: 1px solid var(--grim-line);
    border-radius: 7px;
    outline: none;
  }

  .scope-filter:focus-visible {
    border-color: var(--grim-accent);
  }

  .scope-list {
    list-style: none;
    margin: 0;
    padding: 0;
    max-height: min(46vh, 340px);
    overflow-y: auto;
  }

  .scope-group-head {
    font-family: var(--font-mono);
    font-size: 9.5px;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--grim-text-faint);
    padding: 8px 8px 3px;
  }

  .scope-option {
    padding: 7px 9px;
    border-radius: 6px;
    cursor: pointer;
    font-family: var(--grim-serif);
    font-size: 14px;
    color: var(--grim-ink);
  }

  .scope-option.active {
    background: var(--grim-accent-soft);
  }

  .scope-option.selected {
    color: var(--grim-accent);
    font-weight: 600;
  }

  .scope-empty {
    padding: 10px 9px;
    font-size: 13px;
    color: var(--grim-text-faint);
  }
</style>
