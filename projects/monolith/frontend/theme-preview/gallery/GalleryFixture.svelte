<script>
  import {
    Breadcrumb,
    Button,
    Disclosure,
    Field,
    PageHeader,
    Tabs,
  } from "@homelab/design-system/components";
  import {
    ChartFrame,
    KeyValue,
    Legend,
    Metric,
    Panel,
    Status,
    formatMeasurement,
  } from "@homelab/design-system/data-display";
  import "@homelab/design-system/tokens/contract.css";
  import "@homelab/design-system/tokens/technical-drawing.css";
  import "../../factory-preview/fonts.css";
  import { FIXTURES } from "./fixtures.js";

  let { fixtures = FIXTURES } = $props();
  let selected = $state(FIXTURES.initialTab);
  let navigation = $state(FIXTURES.initialTab);
  let open = $state(false);
  let clicks = $state(FIXTURES.count);
  let submissions = $state(FIXTURES.count);
  const copy = $derived(fixtures.text);
  const formatted = (value, unit) =>
    formatMeasurement(value, { unit, locale: fixtures.locale });
</script>

{#snippet breadcrumbs(name, boundary)}
  <Breadcrumb
    items={[
      { label: copy.gallery, href: `#${boundary}-dashboard` },
      { label: name, current: true },
    ]}
  />
{/snippet}

{#snippet chart(state = "ready")}
  <ChartFrame {...fixtures.chart} {state} message={copy.stateMessage}>
    <svg
      class="plot"
      viewBox="0 0 120 120"
      role="img"
      aria-label={copy.chartLabel}
    >
      <title>{fixtures.chart.title}</title>
      {#each fixtures.chart.series as series, index}
        <g
          transform={`translate(10 ${10 + index * 24})`}
          style={`color: var(${series.role})`}
          data-gallery-series={series.id}
        >
          <title
            >{series.label}: {formatted(series.value, fixtures.chart.units)
              .exactText}</title
          >
          <path
            d={`M0 0 H${series.value}`}
            stroke="currentColor"
            stroke-width="3"
          />
          <g transform={`translate(${series.value} 0)`} fill="currentColor">
            {#if series.marker === "circle"}<circle r="4" />
            {:else if series.marker === "square"}<rect
                x="-4"
                y="-4"
                width="8"
                height="8"
              />
            {:else if series.marker === "triangle"}<path d="M0 -5 L5 4 H-5 Z" />
            {:else if series.marker === "diamond"}<path
                d="M0 -5 L5 0 L0 5 L-5 0 Z"
              />
            {:else}<path
                d="M-2 -5 H2 V-2 H5 V2 H2 V5 H-2 V2 H-5 V-2 H-2 Z"
              />{/if}
          </g>
        </g>
      {/each}
    </svg>
    <Legend entries={fixtures.chart.series} label={copy.legend} />
    {#snippet fallback(current)}
      {#if current === "ready"}
        <table data-gallery-fallback>
          <caption
            >{fixtures.chart.title}: {copy.exactValues} ({fixtures.chart
              .units})</caption
          >
          <thead
            ><tr
              ><th scope="col">{copy.tier}</th><th scope="col"
                >{copy.allocation}</th
              ></tr
            ></thead
          >
          <tbody>
            {#each fixtures.chart.series as series}
              <tr
                ><th scope="row">{series.label}</th><td
                  >{formatted(series.value, fixtures.chart.units).exactText}</td
                ></tr
              >
            {/each}
          </tbody>
        </table>
      {:else}
        <p>
          {fixtures.chart.title} ({fixtures.chart.units}): {current}, {copy.noObservations}
        </p>
      {/if}
    {/snippet}
  </ChartFrame>
{/snippet}

{#snippet form(boundary)}
  <form
    onsubmit={(event) => {
      event.preventDefault();
      submissions++;
    }}
  >
    <Field label={copy.region} description={copy.regionDescription} required>
      {#snippet control(attributes)}
        <select {...attributes} name="region"
          >{#each fixtures.regions as region}<option value={region.value}
              >{region.label}</option
            >{/each}</select
        >
      {/snippet}
    </Field>
    <Field
      label={copy.reference}
      description={copy.referenceDescription}
      error={copy.referenceError}
      required
    >
      {#snippet control(attributes)}<input
          {...attributes}
          name="reference"
        />{/snippet}
    </Field>
    <Field
      label={copy.destination}
      description={copy.destinationDescription}
      disabled
    >
      {#snippet control(attributes)}<input
          {...attributes}
          name="destination"
        />{/snippet}
    </Field>
    <div class="actions">
      <Button type="submit" data-gallery-action={`${boundary}-submit`}
        >{copy.save}</Button
      >
      <Button type="reset" variant="secondary">{copy.reset}</Button>
      <Button
        disabled
        data-gallery-action={`${boundary}-disabled`}
        onclick={() => clicks++}>{copy.disabled}</Button
      >
    </div>
    <p data-gallery-state="submissions">
      {copy.submissions}: {formatted(submissions, "").text}
    </p>
  </form>
{/snippet}

{#snippet compositions(boundary, theme)}
  <section
    class="boundary"
    data-gallery-boundary={boundary}
    data-ds-theme={theme}
  >
    <section id={`${boundary}-dashboard`} data-gallery-section="dashboard">
      <PageHeader
        title={fixtures.title}
        level={2}
        description={copy.dashboardDescription}
      >
        {#snippet breadcrumb()}{@render breadcrumbs(
            copy.dashboard,
            boundary,
          )}{/snippet}
        {#snippet actions()}<Button
            onclick={() => clicks++}
            data-gallery-action={`${boundary}-refresh`}
            >{copy.inspect}: {formatted(clicks, "").text}</Button
          >{/snippet}
      </PageHeader>
      <Tabs tabs={fixtures.tabs} label={copy.views} bind:selected>
        {#snippet panel(tab)}
          {#if tab.id === "overview"}
            <div class="stack">
              <Panel title={copy.observations} headingLevel={3}>
                <div class="metrics">
                  {#each fixtures.measurements.slice(0, 4) as measurement}<Metric
                      {...measurement}
                      locale={fixtures.locale}
                    />{/each}
                </div>
                <div class="stack">
                  {#each fixtures.statuses as status}<Status
                      {...status}
                    />{/each}
                  <KeyValue rows={fixtures.rows} />
                </div>
              </Panel>
              <Panel title={copy.plot} headingLevel={3}>{@render chart()}</Panel
              >
            </div>
          {:else}<p>
              {copy.notes}
              {fixtures.reference}, {copy.recorded}
              {fixtures.date}.
            </p>{/if}
        {/snippet}
      </Tabs>
    </section>

    <section id={`${boundary}-document`} data-gallery-section="document">
      <PageHeader
        title={copy.documentTitle}
        level={2}
        description={copy.documentDescription}
      >
        {#snippet breadcrumb()}{@render breadcrumbs(
            copy.document,
            boundary,
          )}{/snippet}
      </PageHeader>
      <Panel title={copy.summary} headingLevel={3}>
        <p>
          {copy.summaryText}
        </p>
        <h4>{copy.method}</h4>
        <p>
          {copy.methodText}
        </p>
        <KeyValue rows={fixtures.rows} density="sparse" />
        <Disclosure>
          {#snippet summary()}{copy.methodology}{/snippet}
          <p>
            {copy.fixedDate}
            {fixtures.date}. {copy.fixedSource}
          </p>
        </Disclosure>
        <Disclosure>
          {#snippet summary()}{copy.wrappedReference}{/snippet}
          <p>{fixtures.title}</p>
        </Disclosure>
        {@render form(`${boundary}-document`)}
      </Panel>
    </section>

    <section data-gallery-section="controls">
      <Panel title={copy.controls} headingLevel={2}>
        {@render form(`${boundary}-controls`)}
        <Disclosure bind:open>
          {#snippet summary()}{copy.expandable}{/snippet}
          <p>{copy.interactions}</p>
        </Disclosure>
        <div class="actions">
          <Button
            variant="quiet"
            onclick={() => (open = !open)}
            data-gallery-action={`${boundary}-disclosure`}>{copy.toggle}</Button
          >
          <Button variant="secondary" onclick={() => clicks++}
            >{copy.count}: {formatted(clicks, "").text}</Button
          >
        </div>
        <p data-gallery-state="disclosure">{copy.open}: {open}</p>
      </Panel>
    </section>

    <section data-gallery-section="navigation">
      <PageHeader title={copy.navigation} level={2}>
        {#snippet breadcrumb()}{@render breadcrumbs(
            fixtures.title,
            boundary,
          )}{/snippet}
      </PageHeader>
      <Tabs
        tabs={fixtures.tabs}
        label={copy.navigationExamples}
        bind:selected={navigation}
      >
        {#snippet panel(tab)}<p>
            {tab.label}: {copy.panelContent}
          </p>{/snippet}
      </Tabs>
      <p data-gallery-state="selection">{copy.selected}: {navigation}</p>
    </section>

    <section class="stack" data-gallery-section="rows">
      <Panel title={copy.metadata} headingLevel={2}
        ><KeyValue rows={fixtures.rows} density="dense" /></Panel
      >
      {#each fixtures.states as state}
        <div data-gallery-content-state={state}>
          <Panel
            title={`${copy.panel} ${state}`}
            headingLevel={3}
            {state}
            message={copy.contentState}
          >
            <KeyValue rows={fixtures.rows} density="sparse" />
          </Panel>
        </div>
      {/each}
    </section>

    <section data-gallery-section="status">
      <Panel title={copy.statuses} headingLevel={2}>
        <ul class="stack">
          {#each fixtures.statuses as status}<li>
              <Status {...status} />
            </li>{/each}
        </ul>
      </Panel>
    </section>

    <section data-gallery-section="metrics">
      <Panel title={copy.measurements} headingLevel={2}>
        <div class="metrics">
          {#each fixtures.measurements as measurement}<Metric
              {...measurement}
              locale={fixtures.locale}
            />{/each}
          {#each fixtures.states as state}<Metric
              label={`${copy.measurement} ${state}`}
              value={fixtures.count}
              unit={fixtures.countUnit}
              {state}
              locale={fixtures.locale}
            />{/each}
        </div>
      </Panel>
    </section>

    <section data-gallery-section="chart">
      <Panel title={copy.chart} headingLevel={2}>
        <div class="stack">
          {#each fixtures.states as state}<div data-gallery-chart-state={state}>
              {@render chart(state)}
            </div>{/each}
        </div>
      </Panel>
    </section>

    {#if boundary === "light"}
      <section
        class="boundary"
        data-gallery-boundary="nested"
        data-ds-theme="technical-drawing-dark"
      >
        <Panel title={copy.nested} headingLevel={2}>
          <p>
            {copy.nestedDescription}
          </p>
          <Status {...fixtures.statuses[0]} />
          <Metric
            label={copy.nestedZero}
            value={fixtures.count}
            unit={fixtures.countUnit}
            locale={fixtures.locale}
          />
          <Button variant="secondary">{copy.nestedAction}</Button>
        </Panel>
      </section>
      <section data-gallery-boundary="sibling">
        <Panel title={copy.sibling} headingLevel={2}
          ><p>{copy.siblingDescription}</p></Panel
        >
      </section>
    {/if}
  </section>
{/snippet}

<main class="gallery">
  <h1>{copy.title}</h1>
  <p>{copy.introduction}</p>
  <section class="boundary" data-gallery-boundary="unmarked">
    <PageHeader title={copy.unmarked} level={2} />
    <KeyValue rows={fixtures.rows.slice(0, 2)} />
    <Button variant="secondary">{copy.unmarkedAction}</Button>
  </section>
  {@render compositions("light", "technical-drawing-light")}
  {@render compositions("dark", "technical-drawing-dark")}
</main>

<style>
  :global(body) {
    margin: 0;
  }
  .gallery {
    color: var(--ds-ink);
    background: var(--ds-surface);
    font-family: var(--ds-font-body);
  }
  .boundary {
    /* Re-resolve inherited properties at each explicit theme boundary. */
    color: var(--ds-ink);
    background: var(--ds-surface);
    font-family: var(--ds-font-body);
  }
  .gallery,
  .boundary,
  .stack {
    display: grid;
    gap: var(--ds-space-lg);
    min-width: 0;
  }
  .gallery,
  .boundary {
    padding: var(--ds-space-md);
  }
  .metrics {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(100%, 18rem), 1fr));
    gap: var(--ds-space-md);
  }
  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--ds-space-sm);
    margin-block: var(--ds-space-md);
  }
  h1,
  h4,
  p {
    margin: 0 0 var(--ds-space-sm);
    overflow-wrap: anywhere;
    line-height: 1.5;
  }
  ul {
    list-style: none;
    padding: 0;
    margin: 0;
  }
  .plot {
    display: block;
    width: 100%;
    max-width: 30rem;
    height: auto;
    margin-block: var(--ds-space-md);
  }
  table {
    width: 100%;
    table-layout: fixed;
    border-collapse: collapse;
  }
  caption {
    text-align: start;
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
</style>
