<script>
  // Two-state segmented toggle (e.g. Map / Photos). `value` is bindable; options
  // is [{ value, label }, ...].
  let { value = $bindable(), options = [] } = $props();
</script>

<div class="toggle" role="group" aria-label="View">
  {#each options as opt (opt.value)}
    <button
      type="button"
      class:active={value === opt.value}
      aria-pressed={value === opt.value}
      onclick={() => (value = opt.value)}
    >
      {opt.label}
    </button>
  {/each}
</div>

<style>
  .toggle {
    display: inline-flex;
    border: 2px solid var(--ink);
    background: var(--paper);
    font-family: var(--mono);
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }
  .toggle button {
    padding: 8px 14px;
    background: var(--paper);
    border: none;
    color: var(--ink);
    cursor: pointer;
  }
  .toggle button + button {
    border-left: 2px solid var(--ink);
  }
  .toggle button.active {
    background: var(--ink);
    color: var(--paper);
  }
</style>
