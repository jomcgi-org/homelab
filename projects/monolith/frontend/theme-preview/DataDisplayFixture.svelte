<script>
  import {
    Panel,
    Section,
    KeyValue,
    Status,
    Metric,
    ChartFrame,
    Legend,
    DATA_DISPLAY_FIXTURES,
    STATUS_KINDS,
    SERIES_ROLES,
    formatMeasurement,
  } from "@homelab/design-system/data-display";

  let { locale = "en-US" } = $props();
  const measurements = DATA_DISPLAY_FIXTURES.measurements;
</script>

<div class="data-display" data-data-display>
  <Panel title="Synthetic data display" headingLevel={3}>
    <div class="metrics">
      {#each measurements as measurement}
        <Metric
          {...measurement}
          {locale}
          context="Synthetic measurement, not live telemetry"
        />
      {/each}
    </div>
    <ul class="statuses" aria-label="All synthetic status meanings">
      {#each Object.entries(STATUS_KINDS) as [kind, status]}
        <li><Status {kind} label={`${status.label}: ${status.meaning}`} /></li>
      {/each}
    </ul>
    {#each DATA_DISPLAY_FIXTURES.densities as density}
      <Section title={`${density} rows`} headingLevel={4}>
        <KeyValue rows={DATA_DISPLAY_FIXTURES.rows} {density} />
      </Section>
    {/each}
    <ChartFrame
      title="Synthetic memory tiers"
      units="LongSyntheticUnitWithoutBreaksForWrapping"
      description="Synthetic edge measurements in fixture order. Shape and label identify each series; the table contains exact values."
    >
      <Legend entries={[...SERIES_ROLES].reverse()} />
      {#snippet fallback(state)}
        <table data-fallback-table>
          <caption>Synthetic edge measurements ({state})</caption>
          <thead
            ><tr
              ><th scope="col">Measurement</th><th scope="col"
                >Exact value and unit</th
              ></tr
            ></thead
          >
          <tbody>
            {#each measurements as measurement}
              <tr
                ><th scope="row">{measurement.label}</th><td
                  >{formatMeasurement(measurement.value, {
                    unit: measurement.unit,
                    locale,
                  }).exactText}</td
                ></tr
              >
            {/each}
          </tbody>
        </table>
      {/snippet}
    </ChartFrame>
    {#snippet footer()}Synthetic footer partition, no live service{/snippet}
  </Panel>
  {#each DATA_DISPLAY_FIXTURES.states as state}
    <div class="state-case" data-state-case={state}>
      <Panel
        title={`Panel ${state}`}
        headingLevel={3}
        {state}
        message="Synthetic state explanation"
      />
      <Metric label={`Metric ${state}`} value={0} unit="bytes" {state} />
      <ChartFrame
        title={`Chart ${state}`}
        units="bytes"
        description={`Synthetic ${state} chart, no live data`}
        {state}
        message="Synthetic state explanation"
      >
        <p>Hidden stale chart</p>
        {#snippet fallback(current)}<p>
            Chart data: {current}, no measurements available.
          </p>{/snippet}
      </ChartFrame>
    </div>
  {/each}
</div>

<style>
  .data-display {
    display: grid;
    gap: var(--ds-space-md);
    margin-top: var(--ds-space-md);
    min-width: 0;
  }
  .metrics {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: var(--ds-space-md);
    margin-bottom: var(--ds-space-md);
  }
  .statuses {
    list-style: none;
    padding: 0;
    margin-block: var(--ds-space-md);
  }
  li {
    margin-block: var(--ds-space-sm);
  }
  .state-case {
    display: grid;
    gap: var(--ds-space-md);
  }
  table {
    width: 100%;
    table-layout: fixed;
    border-collapse: collapse;
    font-size: 1rem;
  }
  caption {
    text-align: start;
    font-weight: 700;
    margin-bottom: var(--ds-space-sm);
  }
  th,
  td {
    text-align: start;
    vertical-align: top;
    overflow-wrap: anywhere;
    padding: var(--ds-space-xs);
    border-bottom: var(--ds-border-weight) solid var(--ds-line);
  }
  p {
    overflow-wrap: anywhere;
  }
  @media (max-width: 700px) {
    .metrics {
      grid-template-columns: minmax(0, 1fr);
    }
  }
</style>
