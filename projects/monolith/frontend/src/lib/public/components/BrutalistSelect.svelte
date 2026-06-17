<script>
  // A custom select styled to the MotherDuck-brutalist house language: a native
  // <select> can render its closed box with CSS but the OPEN option list is OS
  // chrome (rounded, gradient, system font) with no styling hooks, so we rebuild
  // the control. That means re-implementing what the native element gives for
  // free: the WAI-ARIA select-only combobox pattern (focus stays on the trigger,
  // a roving aria-activedescendant highlights options), full keyboard nav,
  // type-ahead, and click-outside. Anything less would be less usable than the
  // <select> it replaces.
  //
  // Props:
  //   options  -> [{ value, label }]
  //   value    -> bindable selected value (matches an option.value)
  //   onchange -> called after the value changes (parity with <select onchange>)
  //   label    -> accessible name for the listbox
  //   id       -> base id for stable option ids (aria-activedescendant)
  let {
    options = [],
    value = $bindable(""),
    onchange = () => {},
    label = "Select",
    id = "brutalist-select",
  } = $props();

  let open = $state(false);
  // Index of the keyboard-highlighted option while open (the roving focus). -1
  // means "none yet"; opening seeds it to the current selection.
  let activeIndex = $state(-1);

  let rootEl;
  let listEl;

  let selectedIndex = $derived(options.findIndex((o) => o.value === value));
  let selectedLabel = $derived(
    selectedIndex >= 0 ? options[selectedIndex].label : "",
  );

  const optionId = (i) => `${id}-opt-${i}`;

  function openList() {
    open = true;
    // Seed the highlight on the current selection so arrow keys move from there.
    activeIndex = selectedIndex >= 0 ? selectedIndex : 0;
  }

  function closeList() {
    open = false;
    activeIndex = -1;
  }

  function commit(i) {
    if (i < 0 || i >= options.length) return;
    const next = options[i].value;
    const changed = next !== value;
    value = next;
    closeList();
    // Mirror native <select>: onchange fires only when the value actually moves.
    if (changed) onchange();
  }

  // Scroll the highlighted option into view when arrowing through a long list
  // (the region list overflows its max-height).
  $effect(() => {
    if (!open || activeIndex < 0 || !listEl) return;
    const el = listEl.querySelector(`#${CSS.escape(optionId(activeIndex))}`);
    el?.scrollIntoView({ block: "nearest" });
  });

  // Type-ahead: native selects let you jump by typing a prefix. Accumulate
  // keystrokes within a short window and match option labels.
  let typeBuffer = "";
  let typeTimer = null;
  function typeAhead(char) {
    typeBuffer += char.toLowerCase();
    clearTimeout(typeTimer);
    typeTimer = setTimeout(() => (typeBuffer = ""), 600);
    const from = (open ? activeIndex : selectedIndex) + 1;
    // Search wrapping from just after the current position so repeated presses
    // cycle through same-letter matches.
    for (let k = 0; k < options.length; k++) {
      const i = (from + k) % options.length;
      if (options[i].label.toLowerCase().startsWith(typeBuffer)) {
        if (open) activeIndex = i;
        else commit(i);
        return;
      }
    }
  }

  function onTriggerKeydown(e) {
    switch (e.key) {
      case "ArrowDown":
      case "ArrowUp":
      case "Enter":
      case " ":
        e.preventDefault();
        if (!open) openList();
        else if (e.key === "Enter" || e.key === " ") commit(activeIndex);
        else activeIndex = clampMove(e.key === "ArrowDown" ? 1 : -1);
        break;
      case "Home":
        if (open) {
          e.preventDefault();
          activeIndex = 0;
        }
        break;
      case "End":
        if (open) {
          e.preventDefault();
          activeIndex = options.length - 1;
        }
        break;
      case "Escape":
        if (open) {
          e.preventDefault();
          closeList();
        }
        break;
      case "Tab":
        // Let focus leave naturally, but don't strand an open popup behind it.
        if (open) closeList();
        break;
      default:
        // Single printable char -> type-ahead.
        if (e.key.length === 1 && !e.metaKey && !e.ctrlKey && !e.altKey) {
          e.preventDefault();
          if (!open) openList();
          typeAhead(e.key);
        }
    }
  }

  function clampMove(delta) {
    const n = options.length;
    if (n === 0) return -1;
    const start = activeIndex < 0 ? selectedIndex : activeIndex;
    return Math.max(0, Math.min(n - 1, (start < 0 ? 0 : start) + delta));
  }

  function onWindowPointerDown(e) {
    if (open && rootEl && !rootEl.contains(e.target)) closeList();
  }
</script>

<svelte:window onpointerdown={onWindowPointerDown} />

<div class="bsel" bind:this={rootEl}>
  <button
    type="button"
    class="bsel-trigger"
    class:open
    role="combobox"
    aria-haspopup="listbox"
    aria-expanded={open}
    aria-controls={`${id}-listbox`}
    aria-activedescendant={open && activeIndex >= 0
      ? optionId(activeIndex)
      : undefined}
    aria-label={label}
    onclick={() => (open ? closeList() : openList())}
    onkeydown={onTriggerKeydown}
  >
    <span class="bsel-value">{selectedLabel}</span>
    <span class="bsel-caret" aria-hidden="true">{open ? "▴" : "▾"}</span>
  </button>

  {#if open}
    <ul
      class="bsel-list"
      id={`${id}-listbox`}
      role="listbox"
      aria-label={label}
      bind:this={listEl}
      tabindex="-1"
    >
      {#each options as opt, i (opt.value)}
        <li
          id={optionId(i)}
          class="bsel-option"
          class:active={i === activeIndex}
          class:selected={opt.value === value}
          role="option"
          aria-selected={opt.value === value}
          onpointerenter={() => (activeIndex = i)}
          onpointerdown={(e) => {
            // Mouse only: prevent the mousedown from blurring the trigger so
            // focus stays on the combobox. Do NOT preventDefault or commit on
            // touch: committing here tears the list out of the DOM before the
            // tap's synthesized click lands, so that click falls through to the
            // job row beneath the dropdown (a ghost click that opened the job).
            // Committing on click instead keeps the option mounted through the
            // whole tap, so the click is consumed here and never reaches the row.
            if (e.pointerType === "mouse") e.preventDefault();
          }}
          onclick={() => commit(i)}
        >
          {#if opt.value === value}
            <span class="bsel-tick" aria-hidden="true">▸</span>
          {:else}
            <span class="bsel-tick bsel-tick-empty" aria-hidden="true"></span>
          {/if}
          <span class="bsel-label">{opt.label}</span>
        </li>
      {/each}
    </ul>
  {/if}
</div>

<style>
  .bsel {
    position: relative;
    width: 100%;
  }

  /* Closed control: same flat sharp box as .field input/select had. */
  .bsel-trigger {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    width: 100%;
    font-family: var(--mono);
    font-size: 13px;
    text-align: left;
    padding: 7px 8px;
    background: var(--cream);
    border: 2px solid var(--ink);
    color: var(--ink);
    cursor: pointer;
    /* Kill the translucent grey/blue flash mobile browsers paint on tap; the
       brutalist controls own their own press feedback. */
    -webkit-tap-highlight-color: transparent;
    transition: background 110ms ease;
  }

  /* A select EXPANDS, so highlight it (accent fill) rather than lifting it off
     the page; a raised/shadowed box reads as "floats above", contradicting the
     list that drops below. Mirrors the .open "anchored, not lifted" intent.
     Hover-capable pointers only: touch browsers emulate :hover on tap and the
     yellow wash sticks on the closed control after selecting, reading as an
     unwanted highlight. */
  @media (hover: hover) {
    .bsel-trigger:hover {
      background: var(--accent);
    }
  }

  /* Brutalist focus: the accent highlight, not the OS blue glow. */
  .bsel-trigger:focus-visible {
    outline: none;
    background: var(--accent);
  }

  /* Open: keep the box "pressed in" (no lift) so it reads as anchored to the
     list dropping below it. */
  .bsel-trigger.open {
    transform: none;
    box-shadow: none;
    background: var(--ink);
    color: var(--paper);
  }

  .bsel-value {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .bsel-caret {
    flex: none;
    font-size: 10px;
  }

  /* The open list: a stacked brutalist card. 2px ink border + hard offset
     shadow, no radius, no gradient. Overlaps the trigger's bottom border by
     2px (margin-top: -2px) so the two borders fuse into one continuous edge. */
  .bsel-list {
    position: absolute;
    top: 100%;
    left: 0;
    right: 0;
    margin: -2px 0 0;
    padding: 0;
    list-style: none;
    z-index: 20;
    max-height: 280px;
    overflow-y: auto;
    background: var(--paper);
    border: 2px solid var(--ink);
    box-shadow: 4px 4px 0 var(--ink);
  }

  .bsel-option {
    display: flex;
    align-items: center;
    gap: 8px;
    font-family: var(--mono);
    font-size: 13px;
    letter-spacing: 0.02em;
    padding: 7px 9px;
    color: var(--ink);
    cursor: pointer;
    -webkit-tap-highlight-color: transparent;
    border-bottom: 1px solid var(--cream);
  }

  .bsel-option:last-child {
    border-bottom: none;
  }

  /* Keyboard highlight and pointer hover share the ink-inversion used by
     .day-chip.active / .more-toggle.on, so highlight reads the same everywhere. */
  .bsel-option.active {
    background: var(--ink);
    color: var(--paper);
  }

  /* Selected (but not highlighted) option: yellow accent marker bar, the only
     spot the brand accent appears among these controls. */
  .bsel-option.selected:not(.active) {
    background: var(--accent);
  }

  .bsel-tick {
    flex: none;
    width: 0.7em;
    font-size: 11px;
    line-height: 1;
  }

  .bsel-tick-empty {
    visibility: hidden;
  }

  .bsel-label {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  @media (prefers-reduced-motion: reduce) {
    .bsel-trigger {
      transition: none;
    }
  }
</style>
