<script>
  import { requireKind, requireText } from "./contracts.js";

  // Rows are presentation inputs: { label, value, unit? }, not a data adapter.
  let { rows = [], density = "dense", value: valueSnippet } = $props();
  const checkedDensity = $derived(
    requireKind(density, { dense: true, sparse: true }, "density"),
  );
  function displayValue(value) {
    return value == null ||
      (typeof value === "number" && !Number.isFinite(value))
      ? "Unavailable"
      : String(value);
  }
</script>

<dl data-density={checkedDensity}>
  {#each rows as row}
    <div class="row">
      <dt>{requireText(row.label, "row label")}</dt>
      <dd>
        {#if valueSnippet}{@render valueSnippet(row)}{:else}{displayValue(
            row.value,
          )}{/if}
        {#if row.unit}<span class="unit">{row.unit}</span>{/if}
      </dd>
    </div>
  {/each}
</dl>

<style>
  dl {
    margin: 0;
    min-width: 0;
    color: var(--ds-ink);
    font-family: var(--ds-font-body);
  }
  .row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 2fr);
    gap: var(--ds-space-sm);
    padding-block: var(--ds-space-xs);
  }
  .row + .row {
    border-top: var(--ds-border-weight) solid var(--ds-line);
  }
  dt,
  dd {
    min-width: 0;
    margin: 0;
    overflow-wrap: anywhere;
    text-align: start;
  }
  dt {
    font-family: var(--ds-font-mono);
    color: var(--ds-ink-muted);
  }
  .unit {
    margin-inline-start: 0.25em;
    overflow-wrap: anywhere;
  }
  [data-density="sparse"] .row {
    padding-block: var(--ds-space-md);
  }
  @media (max-width: 30rem) {
    .row {
      grid-template-columns: minmax(0, 1fr);
      gap: var(--ds-space-xs);
    }
  }
</style>
