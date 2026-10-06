<script>
  let {
    label,
    description = "",
    error = "",
    required = false,
    disabled = false,
    control,
  } = $props();
  const uid = $props.id();
  const id = `${uid}-control`;
  const descriptionId = `${uid}-description`;
  const errorId = `${uid}-error`;
  let attributes = $derived({
    id,
    "aria-describedby":
      [description && descriptionId, error && errorId]
        .filter(Boolean)
        .join(" ") || undefined,
    "aria-invalid": error ? "true" : undefined,
    required,
    disabled,
  });
</script>

<div class="field">
  <label for={id}>
    {label}{#if required}<span>{" (required)"}</span>{/if}
  </label>
  {#if description}<p id={descriptionId} class="description">
      {description}
    </p>{/if}
  {@render control(attributes)}
  {#if error}<p id={errorId} class="error">Error: {error}</p>{/if}
</div>

<style>
  .field {
    display: grid;
    min-width: 0;
    gap: var(--ds-space-xs);
    color: var(--ds-ink);
    font-family: var(--ds-font-body);
    overflow-wrap: anywhere;
  }
  label {
    font-weight: 600;
  }
  label span,
  .description {
    color: var(--ds-ink-muted);
  }
  p {
    margin: 0;
  }
  .error {
    color: var(--ds-err);
  }
  .field :global(input),
  .field :global(select),
  .field :global(textarea) {
    box-sizing: border-box;
    min-width: 44px;
    min-height: 44px;
    width: 100%;
    max-width: 100%;
    padding: var(--ds-space-xs);
    border: var(--ds-border-weight) solid var(--ds-ink);
    border-radius: var(--ds-radius);
    background: var(--ds-surface-raised);
    color: var(--ds-ink);
    font: inherit;
  }
  .field :global(:disabled) {
    border-style: dashed;
    cursor: not-allowed;
  }
  .field :global(:focus-visible) {
    outline: var(--ds-focus-width, var(--ds-border-weight)) solid
      var(--ds-focus, var(--ds-ink));
    outline-offset: 3px;
  }
  @media (prefers-reduced-motion: reduce) {
    .field :global(input),
    .field :global(select),
    .field :global(textarea) {
      transition: none;
      animation: none;
    }
  }
</style>
