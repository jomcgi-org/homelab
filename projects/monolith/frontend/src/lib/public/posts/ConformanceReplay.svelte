<script>
  import recording from "./conformance-replay.json";

  // One step per trace record, not per millisecond: the trace is bursty (a
  // prime, a dispatch and a heartbeat land within 40 ms, then five seconds
  // of nothing) and a time scrub would spend most of its travel on
  // heartbeats. The clock shown is the record's own offset from suite start.
  const events = recording.events;
  const lastIndex = events.length - 1;
  let index = $state(0);
  let playing = $state(false);
  let selected = $state(null);
  let event = $derived(events[index]);
  let complete = $derived(index >= lastIndex);
  let verdicts = $derived(event.verdicts);
  let seen = $derived(events.slice(0, index + 1));

  // The brick's VMs as the trace has described them so far. A prime adds a
  // VM, a dispatch marks it running, a success leaves it finished, and the
  // destroy pair walks it out. The runner also destroys VMs the window never
  // saw primed (they were primed before the window opened), so a destroy of
  // an unknown VM adds it in its terminal state.
  let vms = $derived.by(() => {
    const byId = new Map();
    for (const e of seen) {
      const id = e.vars.vm;
      if (!id) continue;
      const vm = byId.get(id) ?? { id, lane: e.vars.lane, state: "primed" };
      if (e.action === "prime")
        Object.assign(vm, { lane: e.vars.lane, state: "primed" });
      else if (e.action === "dispatch_miss") vm.state = "running";
      else if (e.action === "succeed") vm.state = "finished";
      else if (e.action === "begin_destroy")
        Object.assign(vm, { lane: "session", state: "destroying" });
      else if (e.action === "confirm_destroy")
        Object.assign(vm, { lane: "session", state: "destroyed" });
      byId.set(id, vm);
    }
    return [...byId.values()];
  });

  const seconds = (ms) => (ms / 1000).toFixed(1) + " s";
  function describe(e) {
    const v = e.vars;
    switch (e.action) {
      case "prime":
        return `${v.vm} primed for the ${v.lane} lane (${v.workload})`;
      case "dispatch_miss":
        return `task ${v.task} dispatched to ${v.vm}, provenance ${v.provenance}`;
      case "succeed":
        return `task ${v.task} succeeded on ${v.vm}`;
      case "begin_destroy":
        return `destroy of ${v.vm} recorded as intended (session ${v.session})`;
      case "confirm_destroy":
        return `node confirmed ${v.confirmed_by} of ${v.vm}`;
      case "checkpoint":
        return `checkpoint: node reports ${v.live_vms} live, ${v.known} known to the control plane`;
      case "recv_status":
        return v.primed.length
          ? `node ${v.health}, primed ${v.primed.join(", ")}`
          : `node ${v.health}`;
      default:
        return e.action;
    }
  }
  function toggle() {
    if (complete) index = 0;
    playing = !playing;
  }
  $effect(() => {
    if (!playing) return;
    const timer = setInterval(() => {
      index = Math.min(lastIndex, index + 1);
      if (index >= lastIndex) playing = false;
    }, 320);
    return () => clearInterval(timer);
  });
  let log;
  $effect(() => {
    // Keep the newest record in view while scrubbing or playing.
    void index;
    if (log) log.scrollTop = log.scrollHeight;
  });
</script>

<section
  class="replay"
  aria-label="Recorded conformance run on the dev cluster"
>
  <div class="controls">
    <button
      type="button"
      onclick={toggle}
      onpointerdown={(event) => (event.currentTarget.dataset.pointer = "true")}
      onkeydown={(event) => delete event.currentTarget.dataset.pointer}
      onblur={(event) => delete event.currentTarget.dataset.pointer}
      >{playing ? "Pause" : complete ? "Replay" : "Play"}</button
    >
    <label class="timeline"
      ><span class="sr-only">Trace record</span><input
        type="range"
        min="0"
        max={lastIndex}
        step="1"
        bind:value={index}
        oninput={() => (playing = false)}
        aria-valuetext={`record ${index + 1} of ${events.length}, ${seconds(event.at)}`}
      /></label
    >
    <span class="time">{seconds(event.at)}</span>
  </div>

  <div class="instrument">
    <header class="instrument-heading">
      <span
        >Chart {recording.chartVersion}, run {recording.runId.slice(0, 8)}</span
      >
      <span
        class="phase"
        data-phase={complete ? "Complete" : "Tracing"}
        role="status"
        ><i></i>{complete
          ? "Suite complete"
          : `Record ${index + 1} of ${events.length}`}</span
      >
    </header>

    <div class="panes">
      <section class="trace" aria-label="Trace records">
        <header>
          <strong>Trace</strong><span>control plane, oldest first</span>
        </header>
        <ol bind:this={log}>
          {#each seen as e (e.seq)}
            <li
              class={e.action}
              aria-current={e === event ? "step" : undefined}
            >
              <span class="at">{seconds(e.at)}</span>
              <span class="action">{e.action}</span>
              <span class="what">{describe(e)}</span>
            </li>
          {/each}
        </ol>
        <header class="brick">
          <strong>VMs on the brick</strong><span
            >as the trace describes them</span
          >
        </header>
        <ul class="vms" aria-label="VMs the trace has mentioned">
          {#each vms as vm (vm.id)}
            <li data-state={vm.state}>
              <span class="id">{vm.id}</span>
              <span class="state">{vm.state}</span>
              <span class="lane">{vm.lane}</span>
            </li>
          {:else}
            <li class="empty">No VM mentioned yet.</li>
          {/each}
        </ul>
      </section>

      <section class="invariants" aria-label="Invariant verdicts">
        <header>
          <strong>Invariants</strong><span
            >checker verdict after this record</span
          >
        </header>
        <ul>
          {#each recording.invariants as inv (inv.key)}
            {@const [verdict, coverage] = verdicts[inv.key]}
            <li>
              <button
                type="button"
                data-verdict={verdict}
                aria-pressed={selected === inv.key}
                onclick={() =>
                  (selected = selected === inv.key ? null : inv.key)}
              >
                <span class="name">{inv.name}</span>
                <span class="verdict"><i></i>{verdict}</span>
                <span class="coverage">{coverage} checked</span>
              </button>
            </li>
          {/each}
        </ul>
        {#if selected}
          {@const inv = recording.invariants.find((i) => i.key === selected)}
          <p class="caption">
            {inv.meaning}
            {#if complete}
              The checker said: "{recording.final[selected][2]}".
            {/if}
          </p>
        {:else}
          <p class="caption">
            Vacuous means the window gave the invariant nothing to check. It is
            reported, never counted as a pass.
          </p>
        {/if}
      </section>
    </div>

    {#if complete}
      <footer class="suite" aria-label="Suite verdict">
        <ol>
          {#each recording.scenarios as s (s.id)}
            <li data-verdict={s.verdict}>
              <span class="id">{s.id}</span>
              <span class="title">{s.title}</span>
              <span class="ms">{(s.ms / 1000).toFixed(1)} s</span>
            </li>
          {/each}
        </ol>
        <p>
          Suite verdict for chart {recording.chartVersion}:
          <strong data-verdict={recording.suiteVerdict}
            >{recording.suiteVerdict}</strong
          >. Kargo reads this.
        </p>
      </footer>
    {/if}
  </div>
  <p class="caption conditions">{recording.conditions}</p>
</section>

<style>
  .replay {
    min-width: 0;
    color: var(--ink);
    font-family: var(--font-ui);
  }
  .replay .caption {
    color: var(--ink-2);
    font-size: 0.75rem;
    line-height: 1.5;
  }
  .conditions {
    margin: 0.8rem 0 0;
  }
  .controls {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    align-items: center;
    margin: 0.9rem 0;
    font-size: 0.75rem;
  }
  .controls > button {
    width: 5.5rem;
    flex-shrink: 0;
  }
  .controls :global(button[data-pointer]:focus) {
    outline: none;
  }
  button {
    font: inherit;
    color: var(--ink);
    background: var(--sheet);
    border: 1px solid var(--stroke);
    padding: 0.5rem 0.65rem;
    border-radius: 3px;
    cursor: pointer;
  }
  button:focus-visible,
  input:focus-visible,
  ol:focus-visible {
    outline: 2px solid var(--accent-ink);
    outline-offset: 3px;
  }
  .timeline {
    flex: 1;
    min-width: 6rem;
    display: flex;
    align-items: center;
  }
  .timeline input {
    width: 100%;
    min-width: 0;
    accent-color: var(--accent-ink);
  }
  .time {
    min-width: 4rem;
    text-align: right;
    font-family: var(--font-code);
  }
  .instrument {
    margin-inline: -1rem;
    border-block: 1px solid var(--stroke);
  }
  .instrument-heading {
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 0.5rem;
    padding: 0.8rem 1rem;
    border-bottom: 1px solid var(--stroke);
    font-size: 0.9rem;
  }
  .phase {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    font: 0.7rem var(--font-code);
  }
  .phase i {
    width: 0.45rem;
    height: 0.45rem;
    border-radius: 50%;
    background: var(--accent);
  }
  .phase[data-phase="Complete"] i {
    background: var(--ok);
  }
  .panes {
    display: grid;
    grid-template-columns: minmax(0, 3fr) minmax(0, 2fr);
  }
  .panes > section + section {
    border-left: 1px solid var(--line);
  }
  .panes header {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    flex-wrap: wrap;
    gap: 0.5rem;
    padding: 0.7rem 1rem;
    font-size: 0.8rem;
    border-bottom: 1px solid var(--line);
  }
  .panes header > span {
    color: var(--ink-2);
    font: 0.7rem var(--font-code);
  }
  .trace ol {
    list-style: none;
    margin: 0;
    padding: 0.4rem 0;
    height: 13rem;
    overflow-y: auto;
    font: 0.7rem var(--font-code);
    scroll-behavior: auto;
  }
  .trace ol li {
    display: grid;
    grid-template-columns: 3.4rem 7.5rem minmax(0, 1fr);
    gap: 0.5rem;
    padding: 0.15rem 1rem;
    line-height: 1.5;
    overflow-wrap: anywhere;
  }
  .trace ol li.recv_status {
    color: var(--ink-3);
  }
  .trace ol li.checkpoint {
    color: var(--ink-2);
  }
  .trace ol li[aria-current="step"] {
    background: var(--band);
    color: var(--ink);
  }
  .trace .action {
    font-weight: 600;
  }
  .trace header.brick {
    border-top: 1px solid var(--line);
  }
  .vms {
    list-style: none;
    margin: 0;
    padding: 0.6rem 1rem 0.8rem;
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem;
    min-height: 3.2rem;
  }
  .vms li {
    display: grid;
    gap: 0.1rem;
    padding: 0.35rem 0.5rem;
    border: 1px solid var(--stroke);
    font: 0.65rem var(--font-code);
    box-shadow: inset 0 3px var(--vm-color, var(--ink-3));
  }
  .vms li[data-state="primed"] {
    --vm-color: var(--ink-3);
  }
  .vms li[data-state="running"] {
    --vm-color: var(--accent-ink);
  }
  .vms li[data-state="finished"] {
    --vm-color: var(--ok);
  }
  .vms li[data-state="destroying"] {
    --vm-color: var(--replay-warm);
  }
  .vms li[data-state="destroyed"] {
    --vm-color: var(--ink-3);
    opacity: 0.55;
  }
  .vms .state {
    font-weight: 600;
  }
  .vms .lane,
  .vms .empty {
    color: var(--ink-2);
  }
  .vms li.empty {
    flex: 1 0 100%;
    border: 0;
    box-shadow: none;
    padding-left: 0;
  }
  .invariants ul {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .invariants li + li {
    border-top: 1px solid var(--line);
  }
  .invariants li button {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 5.2rem 5rem;
    gap: 0.5rem;
    align-items: center;
    width: 100%;
    text-align: left;
    padding: 0.45rem 1rem;
    border: 0;
    border-radius: 0;
    background: transparent;
    font-size: 0.75rem;
  }
  .invariants li button[aria-pressed="true"] {
    background: var(--band);
  }
  .invariants .coverage {
    color: var(--ink-2);
    font: 0.65rem var(--font-code);
    text-align: right;
  }
  .verdict {
    display: flex;
    align-items: center;
    gap: 0.35rem;
    font: 0.7rem var(--font-code);
  }
  .verdict i {
    width: 0.5rem;
    height: 0.5rem;
    border: 1px solid var(--ink-3);
    background: transparent;
  }
  [data-verdict="pass"] .verdict i,
  .suite li[data-verdict="pass"] .id {
    background: var(--ok);
    border-color: var(--ok);
  }
  [data-verdict="fail"] .verdict i,
  .suite li[data-verdict="fail"] .id {
    background: var(--replay-hot);
    border-color: var(--replay-hot);
  }
  [data-verdict="vacuous"] .verdict {
    color: var(--ink-2);
  }
  [data-verdict="vacuous"] .verdict i {
    border-style: dashed;
  }
  .invariants .caption {
    margin: 0;
    padding: 0.7rem 1rem;
    border-top: 1px solid var(--line);
  }
  .suite {
    border-top: 1px solid var(--stroke);
    padding: 0.8rem 1rem;
    font-size: 0.8rem;
  }
  .suite ol {
    list-style: none;
    margin: 0 0 0.6rem;
    padding: 0;
    display: grid;
    grid-template-columns: repeat(5, minmax(0, 1fr));
    border: 1px solid var(--line);
  }
  .suite li {
    display: grid;
    gap: 0.2rem;
    padding: 0.5rem;
    min-width: 0;
    font-size: 0.65rem;
  }
  .suite li + li {
    border-left: 1px solid var(--line);
  }
  .suite .id {
    justify-self: start;
    padding: 0 0.3rem;
    color: var(--sheet);
    font: 0.65rem var(--font-code);
  }
  .suite .title {
    color: var(--ink-2);
    line-height: 1.3;
  }
  .suite .ms {
    font-family: var(--font-code);
  }
  .suite p {
    margin: 0;
  }
  .suite strong[data-verdict="pass"] {
    color: var(--ok);
  }
  .suite strong[data-verdict="fail"] {
    color: var(--replay-hot);
  }
  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
  }
  @media (max-width: 640px) {
    .panes {
      grid-template-columns: minmax(0, 1fr);
    }
    .panes > section + section {
      border-left: 0;
      border-top: 1px solid var(--stroke);
    }
    .trace ol li {
      grid-template-columns: 3rem minmax(0, 1fr);
    }
    .trace .what {
      grid-column: 2;
    }
    .suite ol {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
    .suite li:nth-child(n + 3) {
      border-top: 1px solid var(--line);
    }
    .suite li:nth-child(3) {
      border-left: 0;
    }
    .time {
      min-width: 3rem;
    }
  }
</style>
