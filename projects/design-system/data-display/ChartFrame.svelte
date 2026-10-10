<script>
  import { CONTENT_STATES, requireKind, requireText } from "./contracts.js";

  let {
    title,
    units,
    description,
    state = "ready",
    message,
    children,
    fallback,
  } = $props();
  const componentId = $props.id();
  const titleId = `${componentId}-title`;
  const unitsId = `${componentId}-units`;
  const descriptionId = `${componentId}-description`;
  const checked = $derived.by(() => {
    const metadata = {
      title: requireText(title, "chart title"),
      units: requireText(units, "chart units"),
      description: requireText(description, "chart description"),
      state: requireKind(state, CONTENT_STATES, "content state"),
    };
    if (typeof fallback !== "function")
      throw new TypeError("chart fallback snippet is required");
    return metadata;
  });
</script>

<figure
  aria-labelledby={titleId}
  aria-describedby={`${unitsId} ${descriptionId}`}
  aria-busy={checked.state === "loading" ? true : undefined}
  data-state={checked.state}
>
  <figcaption>
    <p class="title" id={titleId}>{checked.title}</p>
    <p class="units" id={unitsId}>Units: {checked.units}</p>
    <p id={descriptionId}>{checked.description}</p>
  </figcaption>
  {#if checked.state === "ready"}
    <div class="chart">{@render children?.()}</div>
  {:else}
    <p class="state">
      {CONTENT_STATES[checked.state]}{message ? `: ${message}` : ""}
    </p>
  {/if}
  <details>
    <summary>Read chart data</summary>
    <div class="fallback">{@render fallback(checked.state)}</div>
  </details>
</figure>

<style>
  figure {
    margin: 0;
    min-width: 0;
    color: var(--ds-ink);
    font-family: var(--ds-font-body);
    overflow-wrap: anywhere;
  }
  figcaption {
    border-bottom: var(--ds-border-weight) solid var(--ds-line);
  }
  p {
    margin: 0 0 var(--ds-space-sm);
  }
  .title {
    font-family: var(--ds-font-mono);
    font-weight: 700;
  }
  .units,
  .state {
    color: var(--ds-ink-muted);
  }
  [data-state="error"] .state {
    color: var(--ds-err);
  }
  .chart,
  .fallback {
    min-width: 0;
    padding-block: var(--ds-space-sm);
  }
  summary {
    cursor: pointer;
    min-height: 44px;
    box-sizing: border-box;
    padding-block: var(--ds-space-sm);
    font-size: 1rem;
  }
  summary:focus-visible {
    outline: var(--ds-focus-width, 2px) solid var(--ds-focus, currentColor);
    outline-offset: var(--ds-focus-width, 2px);
  }
</style>
