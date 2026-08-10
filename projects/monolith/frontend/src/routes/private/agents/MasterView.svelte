<script>
  import { fmtCost, fmtDur } from "./run-format.js";
  import { RUN_LEXICON as P } from "./run-lexicon.js";
  let { master } = $props();
  const total = $derived(
    master.runs.reduce((sum, run) => sum + Number(run.cost_usd || 0), 0),
  );
</script>

<div class="runview master-view">
  <div class="rv-eyebrow">
    <span class="eyebrow-label">{P.labels.masterEyebrow}</span><span
      class="rv-id">{master.runs.length} {P.labels.inFlight}</span
    >
  </div>
  {#if master.runs.some((run) => run.needs)}<div class="att-band">
      <div class="col-label">{P.labels.attention}</div>
      {#each master.runs.filter((run) => run.needs) as run}<div class="att-row">
          <div class="att-title">
            {run.title}
            <span class="entry-meta">{P.stateWords[run.state]}</span>
          </div>
          <div class="att-reason">{run.needs.reason}</div>
        </div>{/each}
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
      {#each master.runs as run}<div class="m-row">
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
        </div>{/each}
    </div>{:else}<div class="m-quiet">{P.labels.noRuns}</div>{/if}
  <div class="m-totals">
    {P.labels.spend}
    {P.punct.colon}
    {fmtCost(total) || "$0.00"}
    {P.punct.dot}
    {master.runs.length}
    {P.labels.runsWord}
  </div>
</div>
