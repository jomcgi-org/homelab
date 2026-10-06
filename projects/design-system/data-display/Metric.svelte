<script>
  import { CONTENT_STATES, requireKind, requireText } from "./contracts.js";
  import { DEFAULT_LOCALE, formatMeasurement } from "./format.js";

  let {
    label,
    value,
    unit = "",
    locale = DEFAULT_LOCALE,
    context,
    state = "ready",
  } = $props();
  const componentId = $props.id();
  const labelId = `${componentId}-label`;
  const checkedLabel = $derived(requireText(label, "metric label"));
  const checkedState = $derived(
    requireKind(state, CONTENT_STATES, "content state"),
  );
  const formatted = $derived(formatMeasurement(value, { unit, locale }));
  const available = $derived(
    checkedState === "ready" && formatted.state === "available",
  );
  const stateText = $derived(
    checkedState === "ready" ? "Unavailable" : CONTENT_STATES[checkedState],
  );
</script>

<div
  class="metric"
  role="group"
  aria-labelledby={labelId}
  aria-busy={checkedState === "loading" ? true : undefined}
  data-state={available
    ? "ready"
    : checkedState === "ready"
      ? "unavailable"
      : checkedState}
>
  <p class="label" id={labelId}>{checkedLabel}</p>
  {#if available}
    <p class="measurement">
      <span aria-hidden="true"
        >{formatted.text}{#if unit}{" "}<span class="unit">{unit}</span
          >{/if}</span
      >
      <span class="exact-sr">{formatted.exactText}</span>
    </p>
    <details>
      <summary>Exact value</summary>
      <p class="exact">
        <data value={String(value)}>{formatted.exactText}</data>
      </p>
    </details>
  {:else}
    <p class="state">{stateText}</p>
  {/if}
  {#if context}<p class="context">{context}</p>{/if}
</div>

<style>
  .metric {
    min-width: 0;
    color: var(--ds-ink);
    font-family: var(--ds-font-body);
    overflow-wrap: anywhere;
  }
  p {
    margin: 0;
  }
  .label,
  .context,
  .state {
    color: var(--ds-ink-muted);
  }
  .label {
    font-family: var(--ds-font-mono);
  }
  .measurement {
    margin-top: var(--ds-space-xs);
    font-size: 1.5rem;
    line-height: 1.4;
    font-variant-numeric: tabular-nums;
  }
  .unit {
    font-size: 1rem;
    overflow-wrap: anywhere;
  }
  .context {
    margin-top: var(--ds-space-xs);
  }
  [data-state="error"] .state {
    color: var(--ds-err);
  }
  summary {
    cursor: pointer;
    min-height: 44px;
    box-sizing: border-box;
    padding-block: var(--ds-space-sm);
    font-size: 1rem;
  }
  summary:focus-visible {
    outline: 2px solid var(--ds-focus);
    outline-offset: 2px;
  }
  .exact {
    padding-bottom: var(--ds-space-xs);
  }
  .exact-sr {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
    border: 0;
  }
</style>
