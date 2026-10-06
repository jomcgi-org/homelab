<script>
  import {
    Panel,
    KeyValue,
    Metric,
    Status,
    DATA_DISPLAY_FIXTURES,
    STATUS_KINDS,
  } from "@homelab/design-system/data-display";

  let { locale = "en-US" } = $props();
</script>

<div data-ds-theme="technical-drawing-light">
  <Panel title="Synthetic measurements" headingLevel={3}>
    {#each DATA_DISPLAY_FIXTURES.measurements as measurement}
      <Metric {...measurement} {locale} context="Synthetic context" />
    {/each}
    {#each Object.entries(STATUS_KINDS) as [kind, status]}
      <Status {kind} label={status.label} />
    {/each}
    {#each DATA_DISPLAY_FIXTURES.densities as density}
      <KeyValue rows={DATA_DISPLAY_FIXTURES.rows} {density} />
    {/each}
    {#snippet footer()}Synthetic footer partition{/snippet}
  </Panel>
  <div data-ds-theme="technical-drawing-dark">
    <Panel title="Nested dark measurement"
      ><Metric label="Dark zero" value={0} unit="bytes" {locale} /></Panel
    >
  </div>
  {#each DATA_DISPLAY_FIXTURES.states as state}
    <Panel
      title={`Panel ${state}`}
      {state}
      message="Synthetic state explanation"
    >
      <p>Hidden stale content</p>
    </Panel>
    <Metric label={`Metric ${state}`} value={999950} unit="bytes" {state} />
  {/each}
</div>
