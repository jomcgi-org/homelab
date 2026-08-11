<script>
  import { fmtCost, fmtDur } from "./run-format.js";
  import { RUN_LEXICON as P } from "./run-lexicon.js";
  let {
    master,
    onSelectRun = () => {},
    onStartRun = () => {},
    view = { engine_tier: "live", snapshot_age_seconds: 0 },
  } = $props();
  const total = $derived(
    master.runs.reduce((sum, run) => sum + Number(run.cost_usd || 0), 0),
  );
</script>

<div
  class:tier-stale={view.engine_tier === "stale"}
  class="runview master-view"
>
  <div class="rv-eyebrow">
    <span class="eyebrow-label">{P.labels.masterEyebrow}</span><span
      class="rv-id">{master.runs.length} {P.labels.inFlight}</span
    >
  </div>
  {#if view.engine_tier === "absent"}
    <div class="m-quiet">{P.labels.absentNotice}</div>
  {:else}
    {#if master.runs.some((run) => run.needs)}<div class="att-band">
        <div class="col-label">{P.labels.attention}</div>
        {#each master.runs.filter((run) => run.needs) as run}<button
            class="att-row"
            type="button"
            aria-label={`${P.labels.run} ${run.title}${P.punct.colon} ${P.stateWords[run.state] || run.state}`}
            onclick={() => onSelectRun(run.workflow_id)}
          >
            <div class="att-title">
              {run.title}
              <span class="entry-meta">{P.stateWords[run.state]}</span>
            </div>
            <div class="att-reason">{run.needs.reason}</div>
          </button>{/each}
      </div>{:else}<div class="m-quiet">{P.labels.nothingNeedsYou}</div>{/if}
    {#each master.queues as queue}<div class="queue-line">
        {queue.name}
        {P.labels.queueWord}
        {P.punct.dot}
        {queue.running}
        {P.labels.runningWord}
        {P.labels.of}
        {queue.concurrency}
        {P.punct.dot}
        {queue.waiting}
        {P.labels.waitingWord}
      </div>{/each}
    {#if master.runs.length}<div class="m-list">
        {#each master.runs as run}<button
            class="m-row"
            type="button"
            aria-label={`${P.labels.run} ${run.title}${P.punct.colon} ${P.stateWords[run.state] || run.state}`}
            onclick={() => onSelectRun(run.workflow_id)}
          >
            <span class={`state-chip s-${run.state}`}
              >{P.stateWords[run.state] || run.state}</span
            ><span class="m-title">{run.title}</span><span class="m-meta"
              >{run.current
                .label}{#if run.elapsed_seconds != null && run.bound_seconds != null}
                {P.punct.dot}
                {fmtDur(run.elapsed_seconds)}
                {P.labels.of}
                {fmtDur(run.bound_seconds)}{/if}{#if fmtCost(run.cost_usd)}
                {P.punct.dot} {fmtCost(run.cost_usd)}{/if}</span
            >
          </button>{/each}
      </div>{:else}<div class="m-quiet m-empty">
        <span>{P.labels.noRuns}</span>
        <!-- Starting a run is otherwise only reachable by opening the new
             panel and changing its mode select, which reads as "there is no
             way to start one" on a swarm home that is empty by definition
             until the first run exists. -->
        <button class="m-start" type="button" onclick={() => onStartRun()}
          >{P.labels.createRun}</button
        >
      </div>{/if}
    <div class="m-totals">
      {P.labels.spend}
      {P.punct.colon}
      {fmtCost(total) || "$0.00"}
      {P.punct.dot}
      {master.runs.length}
      {P.labels.runsWord}
    </div>
    {#if view.engine_tier === "stale"}<div class="rv-foot">
        {P.labels.engine}{P.punct.colon}
        {P.labels.staleShowing}
        {fmtDur(view.snapshot_age_seconds)}
        {P.labels.staleOld}
      </div>{/if}
  {/if}
</div>

<style>
  /* These two were divs before they became selectable. The UA button border
     and background have to go, but .m-row's bottom hairline comes from
     run-view.css and a blanket `border: 0` here outranks it (scoped styles
     carry the component hash), so it is re-declared rather than reset away. */
  .m-row,
  .att-row {
    width: 100%;
    color: inherit;
    background: transparent;
    text-align: left;
    font: inherit;
    border: 0;
    cursor: pointer;
  }

  .m-row {
    border-bottom: 1px solid var(--line);
  }

  .m-empty {
    display: flex;
    align-items: center;
    gap: 10px;
  }

  .m-start {
    padding: 2px 8px;
    color: var(--info);
    background: transparent;
    font: inherit;
    font-size: var(--size-meta);
    border: 1px solid var(--line-strong);
    cursor: pointer;
  }

  .tier-stale .m-row,
  .tier-stale .att-row,
  .tier-stale .queue-line {
    filter: grayscale(0.6);
    opacity: 0.85;
  }
</style>
