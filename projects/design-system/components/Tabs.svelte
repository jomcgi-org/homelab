<script>
  let {
    tabs,
    label,
    labelledby,
    orientation = "horizontal",
    selected = $bindable(undefined),
    onchange,
    panel,
  } = $props();
  const uid = $props.id();
  let active = $derived(
    tabs.find((tab) => tab.id === selected && !tab.disabled) ??
      tabs.find((tab) => !tab.disabled),
  );

  function select(tab) {
    if (tab.disabled || active?.id === tab.id) return;
    selected = tab.id;
    onchange?.(tab.id);
  }

  function navigate(event, index) {
    const nextKey = orientation === "vertical" ? "ArrowDown" : "ArrowRight";
    const previousKey = orientation === "vertical" ? "ArrowUp" : "ArrowLeft";
    if (![nextKey, previousKey, "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const enabled = tabs
      .map((tab, i) => (!tab.disabled ? i : -1))
      .filter((i) => i >= 0);
    const position = enabled.indexOf(index);
    let next;
    if (event.key === "Home") next = enabled[0];
    else if (event.key === "End") next = enabled.at(-1);
    else {
      const direction = event.key === nextKey ? 1 : -1;
      next = enabled[(position + direction + enabled.length) % enabled.length];
    }
    if (next === undefined) return;
    select(tabs[next]);
    event.currentTarget.parentElement
      .querySelectorAll('[role="tab"]')
      [next].focus();
  }
</script>

<div class="tabs">
  <div
    role="tablist"
    aria-label={labelledby ? undefined : label}
    aria-labelledby={labelledby}
    aria-orientation={orientation}
  >
    {#each tabs as tab, index (tab.id)}
      <button
        type="button"
        role="tab"
        id={`${uid}-tab-${index}`}
        aria-selected={active?.id === tab.id}
        aria-controls={`${uid}-panel-${index}`}
        tabindex={active?.id === tab.id ? 0 : -1}
        disabled={tab.disabled}
        onfocus={() => select(tab)}
        onclick={() => select(tab)}
        onkeydown={(event) => navigate(event, index)}>{tab.label}</button
      >
    {/each}
  </div>
  {#each tabs as tab, index (tab.id)}
    <div
      role="tabpanel"
      id={`${uid}-panel-${index}`}
      aria-labelledby={`${uid}-tab-${index}`}
      tabindex="0"
      hidden={active?.id !== tab.id}
    >
      {@render panel(tab)}
    </div>
  {/each}
</div>

<style>
  .tabs {
    min-width: 0;
    color: var(--ds-ink);
    font-family: var(--ds-font-body);
    overflow-wrap: anywhere;
  }
  [role="tablist"] {
    display: flex;
    flex-wrap: wrap;
    gap: var(--ds-space-xs);
    border-bottom: var(--ds-border-weight) solid var(--ds-line-strong);
  }
  [aria-orientation="vertical"] {
    flex-direction: column;
    align-items: stretch;
  }
  button {
    box-sizing: border-box;
    min-width: 44px;
    min-height: 44px;
    max-width: 100%;
    padding: var(--ds-space-xs) var(--ds-space-sm);
    border: var(--ds-border-weight) solid transparent;
    border-radius: var(--ds-radius);
    background: var(--ds-surface);
    color: var(--ds-ink);
    font: inherit;
    overflow-wrap: anywhere;
    cursor: pointer;
  }
  button[aria-selected="true"] {
    border-bottom-color: var(--ds-ink);
    font-weight: 700;
    text-decoration: underline;
    text-underline-offset: 0.2em;
  }
  button:disabled {
    border-color: var(--ds-ink-muted);
    border-style: dashed;
    color: var(--ds-ink-muted);
    cursor: not-allowed;
  }
  button:focus-visible,
  [role="tabpanel"]:focus-visible {
    outline: var(--ds-focus-width, var(--ds-border-weight)) solid
      var(--ds-focus, var(--ds-ink));
    outline-offset: 3px;
  }
  [role="tabpanel"] {
    padding: var(--ds-space-sm) 0;
  }
  @media (prefers-reduced-motion: reduce) {
    button,
    [role="tabpanel"] {
      transition: none;
      animation: none;
    }
  }
</style>
