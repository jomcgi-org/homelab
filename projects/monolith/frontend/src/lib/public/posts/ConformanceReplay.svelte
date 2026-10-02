<script>
  import recording from "./conformance-replay.json";

  // A time scrub over the suite window. The trace is bursty (a prime, a
  // dispatch and a heartbeat land within 40 ms, then five seconds of
  // heartbeats), so the picture is the timeline itself: every record is a
  // tick, VM lifetimes are bars that grow under the playhead, and the nine
  // invariants fill in as the checker's coverage arrives. Play runs at 8x,
  // so the 57 s window takes about 7 s.
  const events = recording.events;
  const duration = recording.durationMs;
  const RATE = 8;
  let position = $state(0);
  let playing = $state(false);
  let selected = $state(null);
  let current = $derived(events.findLast((e) => e.at <= position) ?? null);
  let verdicts = $derived((current ?? events[0]).verdicts);
  let complete = $derived(position >= duration);
  const pct = (ms) => (100 * ms) / duration + "%";
  const seconds = (ms) => (ms / 1000).toFixed(1) + " s";

  // VM lifetimes as segments. A prime opens a bar; a dispatch, a success
  // and the destroy pair each close the open segment and start the next
  // state. The runner also destroys VMs primed before the window opened,
  // so a destroy of an unknown VM opens its bar at that point.
  // What each VM was doing, from the runner's scenario log for this run.
  const roles = recording.roles ?? {};
  const lanes = (() => {
    const byId = new Map();
    const open = (vm, at, state) => {
      vm.segments.push({ from: at, to: null, state });
    };
    const close = (vm, at) => {
      const last = vm.segments.at(-1);
      if (last && last.to == null) last.to = at;
    };
    const next = {
      prime: "primed",
      dispatch_miss: "running",
      succeed: "finished",
      begin_destroy: "destroying",
      confirm_destroy: "destroyed",
    };
    for (const e of events) {
      const id = e.vars.vm;
      if (!id || !next[e.action]) continue;
      let vm = byId.get(id);
      if (!vm) {
        vm = {
          id,
          lane: e.vars.lane ?? "session",
          role: roles[id] ?? "",
          segments: [],
        };
        byId.set(id, vm);
      }
      if (e.action === "prime") vm.lane = e.vars.lane;
      close(vm, e.at);
      open(vm, e.at, next[e.action]);
    }
    return [...byId.values()];
  })();

  function describe(e) {
    const v = e.vars;
    switch (e.action) {
      case "prime":
        return `a ${v.lane} VM is booted and waiting`;
      case "dispatch_miss":
        return "a task is dispatched to it";
      case "succeed":
        return "the task finishes";
      case "begin_destroy":
        return "the control plane records that it intends to destroy a VM";
      case "confirm_destroy":
        return "the node confirms the VM is gone";
      case "checkpoint":
        return `checkpoint: the node reports ${v.live_vms} live VM${v.live_vms === 1 ? "" : "s"}, the control plane knows ${v.known}`;
      case "recv_status":
        return "node heartbeat";
      default:
        return e.action;
    }
  }
  function toggle() {
    if (complete) position = 0;
    playing = !playing;
  }
  $effect(() => {
    if (!playing) return;
    let previous = performance.now();
    const timer = setInterval(() => {
      const now = performance.now();
      position = Math.min(duration, position + (now - previous) * RATE);
      previous = now;
      if (position >= duration) playing = false;
    }, 40);
    return () => clearInterval(timer);
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
    <span class="time">{seconds(position)}</span>
    <span class="phase" role="status"
      ><i class:complete></i>{complete ? "Run complete" : "Tracing"}</span
    >
  </div>

  <div class="instrument">
    <div class="strip" aria-label="Trace timeline">
      <div class="ticks" aria-hidden="true">
        {#each events as e (e.seq)}
          <i class={e.action} style:left={pct(e.at)}></i>
        {/each}
        <b class="playhead" style:left={pct(position)}></b>
      </div>
      <label class="timeline"
        ><span class="sr-only">Recorded time</span><input
          type="range"
          min="0"
          max={duration}
          step="10"
          bind:value={position}
          oninput={() => (playing = false)}
          aria-valuetext={seconds(position)}
        /></label
      >
      <p class="now">
        {#if current}
          <span class="at">{seconds(current.at)}</span>
          <span class="what">{describe(current)}</span>
        {:else}
          <span class="what">Nothing recorded yet.</span>
        {/if}
      </p>
    </div>

    <div class="lanes" aria-label="VMs on the brick over the window">
      {#each lanes as vm (vm.id)}
        <div class="lane">
          <span class="label"><span class="id">{vm.role || vm.id}</span></span>
          <span class="bar">
            {#each vm.segments as seg}
              {#if seg.from <= position}
                <i
                  data-state={seg.state}
                  style:left={pct(seg.from)}
                  style:width={pct(
                    Math.min(position, seg.to ?? duration) - seg.from,
                  )}
                ></i>
              {/if}
            {/each}
          </span>
        </div>
      {/each}
      <ul class="legend" aria-label="Bar states">
        {#each ["primed", "running", "finished", "destroying", "destroyed"] as state}
          <li data-state={state}><i></i>{state}</li>
        {/each}
      </ul>
    </div>

    <div class="invariants" aria-label="Invariant verdicts">
      {#each recording.invariants as inv (inv.key)}
        {@const [verdict, coverage] = verdicts[inv.key]}
        <button
          type="button"
          data-verdict={verdict}
          aria-pressed={selected === inv.key}
          onclick={() => (selected = selected === inv.key ? null : inv.key)}
        >
          <span class="name">{inv.name}</span>
          <span class="verdict">{verdict}</span>
          <span class="coverage">{coverage}</span>
        </button>
      {/each}
    </div>
    {#if selected}
      {@const inv = recording.invariants.find((i) => i.key === selected)}
      <p class="caption note">
        {inv.meaning}
        {#if complete}
          The checker said: "{recording.final[selected][2]}".
        {/if}
      </p>
    {/if}

    {#if complete}
      <p class="suite" role="status">
        All five scenarios passed in {(
          recording.scenarios.reduce((t, s) => t + s.ms, 0) / 1000
        ).toFixed(0)} s. Verdict
        <strong data-verdict={recording.suiteVerdict}
          >{recording.suiteVerdict}</strong
        >: Kargo promotes the chart.
      </p>
    {/if}
  </div>
</section>

<style>
  .replay {
    --primed: var(--ink-3);
    --running: var(--accent-ink);
    --finished: var(--ok);
    --destroying: var(--replay-warm);
    --destroyed: var(--ink-3);
    min-width: 0;
    color: var(--ink);
    font-family: var(--font-ui);
  }
  .caption {
    color: var(--ink-2);
    font-size: 0.75rem;
    line-height: 1.5;
  }
  .controls {
    display: flex;
    align-items: center;
    gap: 0.75rem;
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
  input:focus-visible {
    outline: 2px solid var(--accent-ink);
    outline-offset: 3px;
  }
  .time {
    min-width: 3.5rem;
    font-family: var(--font-code);
  }
  .phase {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    margin-left: auto;
    font: 0.7rem var(--font-code);
    color: var(--ink-2);
  }
  .phase i {
    width: 0.45rem;
    height: 0.45rem;
    border-radius: 50%;
    background: var(--accent);
  }
  .phase i.complete {
    background: var(--ok);
  }
  .instrument {
    margin-inline: -1rem;
    border-block: 1px solid var(--stroke);
  }
  .strip {
    padding: 0.8rem 1rem 0.6rem;
    border-bottom: 1px solid var(--line);
  }
  .ticks {
    position: relative;
    height: 1.6rem;
    background: var(--band);
    overflow: hidden;
  }
  .ticks i {
    position: absolute;
    bottom: 0;
    width: 1px;
    height: 100%;
    background: var(--ink);
  }
  .ticks i.recv_status {
    height: 35%;
    background: var(--ink-3);
  }
  .ticks i.checkpoint {
    height: 60%;
    background: var(--ink-2);
  }
  .playhead {
    position: absolute;
    top: 0;
    bottom: 0;
    width: 2px;
    margin-left: -1px;
    background: var(--accent-ink);
  }
  .timeline {
    display: block;
    margin-top: 0.15rem;
  }
  .timeline input {
    display: block;
    width: 100%;
    margin: 0;
    accent-color: var(--accent-ink);
  }
  .now {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    min-height: 1.4rem;
    margin: 0.5rem 0 0;
    font: 0.7rem var(--font-code);
    overflow-wrap: anywhere;
  }
  .now .at {
    min-width: 3rem;
    color: var(--ink-2);
  }
  .now .what {
    color: var(--ink-2);
  }
  .lanes {
    padding: 0.6rem 1rem 0.5rem;
    border-bottom: 1px solid var(--line);
  }
  .lane {
    display: grid;
    grid-template-columns: 7.5rem minmax(0, 1fr);
    align-items: center;
    gap: 0.5rem;
    height: 1.35rem;
  }
  .lane .label {
    font: 0.65rem var(--font-code);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .lane .bar {
    position: relative;
    display: block;
    height: 0.8rem;
    background: var(--band);
  }
  .lane .bar i {
    position: absolute;
    top: 0;
    bottom: 0;
    background: var(--ink-3);
  }
  .legend li[data-state="primed"] i,
  .lane .bar i[data-state="primed"] {
    background: var(--primed);
  }
  .legend li[data-state="running"] i,
  .lane .bar i[data-state="running"] {
    background: var(--running);
  }
  .legend li[data-state="finished"] i,
  .lane .bar i[data-state="finished"] {
    background: var(--finished);
    opacity: 0.6;
  }
  .legend li[data-state="destroying"] i,
  .lane .bar i[data-state="destroying"] {
    background: var(--destroying);
  }
  .legend li[data-state="destroyed"] i,
  .lane .bar i[data-state="destroyed"] {
    background: var(--destroyed);
    opacity: 0.4;
  }
  .legend {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem 1rem;
    margin: 0.5rem 0 0;
    padding: 0;
    list-style: none;
    font: 0.65rem var(--font-code);
    color: var(--ink-2);
  }
  .legend li {
    display: flex;
    align-items: center;
    gap: 0.35rem;
  }
  .legend i {
    width: 0.8rem;
    height: 0.5rem;
  }
  .invariants {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 1px;
    background: var(--line);
    border-bottom: 1px solid var(--line);
  }
  .invariants button {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    grid-template-areas: "name coverage" "verdict coverage";
    gap: 0.15rem 0.5rem;
    align-items: center;
    min-width: 0;
    padding: 0.6rem 0.8rem;
    border: 0;
    border-radius: 0;
    text-align: left;
    background: var(--sheet);
    font-size: 0.75rem;
    transition: background-color 240ms ease;
  }
  .invariants .name {
    grid-area: name;
    overflow-wrap: anywhere;
  }
  .invariants .verdict {
    grid-area: verdict;
    font: 0.65rem var(--font-code);
    color: var(--ink-2);
  }
  .invariants .coverage {
    grid-area: coverage;
    font: 1.3rem var(--font-code);
    color: var(--ink-3);
  }
  .invariants button[data-verdict="pass"] {
    background: color-mix(in srgb, var(--ok) 18%, var(--sheet));
  }
  .invariants button[data-verdict="pass"] .coverage {
    color: var(--ink);
  }
  .invariants button[data-verdict="fail"] {
    background: color-mix(in srgb, var(--replay-hot) 18%, var(--sheet));
  }
  .invariants button[data-verdict="vacuous"] .verdict {
    color: var(--ink-3);
  }
  .invariants button[aria-pressed="true"] {
    box-shadow: inset 0 0 0 2px var(--accent-ink);
  }
  .note {
    margin: 0;
    padding: 0.7rem 1rem;
  }
  .suite {
    margin: 0;
    padding: 0.7rem 1rem;
    border-top: 1px solid var(--line);
    font-size: 0.8rem;
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
    .lane {
      grid-template-columns: 6rem minmax(0, 1fr);
    }
    .invariants {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
  }
</style>
