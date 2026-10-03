<script>
  import recording from "./conformance-replay.json";

  // The figure is the system: the runner drives the control plane, the
  // control plane primes and destroys VMs on the brick over gRPC, and every
  // move it makes is a record in the trace store. Play animates the same
  // drawing with a recorded run.
  //
  // Step time, not wall time: state changes sit one unit apart and every
  // other record is placed proportionally between the state changes it
  // fell between. The order is the real run's; the spacing is not, so the
  // 36 s of heartbeats while a session slept is one step like any other.
  const STEP = 1000;
  const isState = (e) =>
    e.action !== "recv_status" && e.action !== "checkpoint";
  const anchors = [
    0,
    ...recording.events.filter(isState).map((e) => e.at),
    recording.durationMs,
  ];
  function stepTime(ms) {
    let i = 0;
    while (i < anchors.length - 2 && ms > anchors[i + 1]) i++;
    const a = anchors[i];
    const b = anchors[i + 1];
    return (i + (b > a ? (ms - a) / (b - a) : 0)) * STEP;
  }
  const events = recording.events.map((e) => ({ ...e, at: stepTime(e.at) }));
  const duration = (anchors.length - 1) * STEP;
  // Short names for the drawing; the recording keeps the runner's own titles.
  const names = {
    S1: "clones over vsock",
    S2: "sleep and relight",
    S3: "second session",
    S4: "invariants",
    S5: "guest round trip",
  };
  let wall = 0;
  const scenarios = recording.scenarios.map((s, i) => {
    const from = wall;
    wall += s.ms;
    return {
      id: s.id,
      n: i + 1,
      title: names[s.id] ?? s.title,
      from: stepTime(from),
      to: stepTime(Math.min(wall, recording.durationMs)),
    };
  });
  const RATE = 1.4;
  const FLIGHT = 650;

  // VM slots on the brick as state segments.
  const nextState = {
    prime: "primed",
    dispatch_miss: "running",
    succeed: "finished",
    begin_destroy: "destroying",
    confirm_destroy: "destroyed",
  };
  const vms = (() => {
    const byId = new Map();
    for (const e of events) {
      const id = e.vars.vm;
      if (!id || !nextState[e.action]) continue;
      let vm = byId.get(id);
      if (!vm) {
        vm = { id, role: recording.roles?.[id] ?? id, segments: [] };
        byId.set(id, vm);
      }
      const last = vm.segments.at(-1);
      if (last && last.to == null) last.to = e.at;
      vm.segments.push({ from: e.at, to: null, state: nextState[e.action] });
    }
    return [...byId.values()];
  })();

  // Edge endpoints in drawing units, and which edge a record travels.
  const P = {
    runner: [232, 100],
    cpIn: [290, 100],
    cpOut: [490, 92],
    brickIn: [548, 92],
    brickOut: [548, 112],
    cpBack: [490, 112],
    writer: [390, 196],
    store: [390, 276],
  };
  const toBrick = new Set(["prime", "dispatch_miss", "begin_destroy"]);
  const fromBrick = new Set(["succeed", "confirm_destroy"]);
  const route = (e) =>
    fromBrick.has(e.action)
      ? [P.brickOut, P.cpBack]
      : toBrick.has(e.action)
        ? [P.cpOut, P.brickIn]
        : null;
  const shown = recording.invariants
    .map((inv, k) => ({ ...inv, k }))
    .filter((inv) => events.at(-1).verdicts[inv.key][1] > 0);
  const traceRows = [
    ["state changes", isState],
    ["checkpoints", (e) => e.action === "checkpoint"],
  ];
  const pct = (ms) => (100 * ms) / duration + "%";

  let position = $state(0);
  let playing = $state(false);
  let complete = $derived(position >= duration);
  let seen = $derived(events.filter((e) => e.at <= position).length);
  let current = $derived(seen ? events[seen - 1] : null);
  let verdicts = $derived((current ?? events[0]).verdicts);
  let dots = $derived.by(() => {
    if (complete) return [];
    const out = [];
    for (const e of events) {
      if (e.action === "recv_status") continue;
      const f = (position - e.at) / FLIGHT;
      if (f < 0 || f > 1) continue;
      const r = route(e);
      if (r) out.push({ path: r, f, small: false });
      out.push({
        path: [P.writer, P.store],
        f,
        small: e.action === "checkpoint",
      });
    }
    for (const s of scenarios) {
      const f = (position - s.from) / FLIGHT;
      if (f >= 0 && f <= 1)
        out.push({ path: [P.runner, P.cpIn], f, small: false });
    }
    return out;
  });
  const slotState = (vm) =>
    vm.segments.findLast((g) => g.from <= position)?.state ?? null;

  // Starting a play brings the whole figure onto the screen first, so the
  // run is watched in one place rather than scrolled after.
  let root;
  function toggle() {
    if (complete) position = 0;
    playing = !playing;
    if (playing && root?.scrollIntoView) {
      const reduce = window.matchMedia?.(
        "(prefers-reduced-motion: reduce)",
      )?.matches;
      root.scrollIntoView({
        behavior: reduce ? "auto" : "smooth",
        block: "start",
      });
    }
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
  bind:this={root}
  aria-label="Trace conformance test, one recorded run"
>
  <div class="cap">
    <span><b>Fig. 1</b> Trace conformance test</span>
    <span class="controls">
      <button
        type="button"
        onclick={toggle}
        onpointerdown={(event) =>
          (event.currentTarget.dataset.pointer = "true")}
        onkeydown={(event) => delete event.currentTarget.dataset.pointer}
        onblur={(event) => delete event.currentTarget.dataset.pointer}
        >{playing ? "Pause" : complete ? "Replay" : "Play"}</button
      >
      <label class="scrub"
        ><span class="sr-only">Position in the run</span><input
          type="range"
          min="0"
          max={duration}
          step="10"
          bind:value={position}
          oninput={() => (playing = false)}
          aria-valuetext={`record ${seen} of ${events.length}`}
        /></label
      >
    </span>
  </div>

  <div class="topo">
    <svg
      viewBox="0 0 780 370"
      role="img"
      aria-label="The runner drives the control plane; the control plane primes and destroys VMs on the brick over gRPC; every move is a trace record"
    >
      <defs
        ><marker
          id="cr-ah"
          viewBox="0 0 8 8"
          refX="7"
          refY="4"
          markerWidth="7"
          markerHeight="7"
          orient="auto"
          ><path d="M0,0 L8,4 L0,8 z" fill="currentColor" /></marker
        ></defs
      >
      <g class="pod">
        <rect x="16" y="20" width="216" height="330" />
        <line x1="16" y1="46" x2="232" y2="46" />
        <text class="t" x="26" y="38">RUNNER</text>
        <text class="sub" x="86" y="38">scenario tests</text>
        {#each scenarios as s (s.id)}
          {@const y = 72 + (s.n - 1) * 58}
          <g
            class="sc"
            class:on={position >= s.from && position < s.to}
            class:done={position >= s.to}
          >
            <rect x="17" y={y - 18} width="214" height="56" />
            {#if s.n < scenarios.length}<line
                x1="16"
                y1={y + 38}
                x2="232"
                y2={y + 38}
              />{/if}
            <text class="id" x="26" y={y + 11}>{s.n}</text>
            <text x="48" y={y + 11}>{s.title}</text>
            <text class="ok" x="222" y={y + 11} text-anchor="end"
              >{position >= s.to ? "✓" : ""}</text
            >
          </g>
        {/each}
      </g>
      <g class="pod">
        <rect x="290" y="20" width="200" height="176" />
        <line x1="290" y1="46" x2="490" y2="46" />
        <text class="t" x="300" y="38">CONTROL PLANE</text>
        <text class="row" x="300" y="74">dispatcher</text>
        <text class="row" x="300" y="96">session manager</text>
        <text class="row" x="300" y="118">node registry</text>
        <line x1="290" y1="136" x2="490" y2="136" />
        <text class="row" x="300" y="160">SpecTrace writer</text>
        <text class="sub" x="300" y="180">one record per action</text>
      </g>
      <g class="pod">
        <rect x="290" y="276" width="200" height="74" />
        <line x1="290" y1="302" x2="490" y2="302" />
        <text class="t" x="300" y="294">TRACE store</text>
        <text class="big" x="300" y="336"
          >{seen} record{seen === 1 ? "" : "s"}</text
        >
      </g>
      <g class="pod">
        <rect x="548" y="20" width="216" height="330" />
        <line x1="548" y1="46" x2="764" y2="46" />
        <text class="t" x="558" y="38">BRICK</text>
        <text class="sub" x="606" y="38">noded + Firecracker</text>
        {#each vms as vm, i (vm.id)}
          {@const x = 560 + (i % 2) * 98}
          {@const y = 64 + Math.floor(i / 2) * 92}
          {@const state = slotState(vm)}
          <g class="slot" data-state={state}>
            <rect {x} {y} width="94" height="72" rx="3" />
            <text x={x + 10} y={y + 26}>{vm.role}</text>
            <text class="st" x={x + 10} y={y + 50}>{state ?? ""}</text>
          </g>
        {/each}
      </g>
      <g class="edge">
        <line x1="232" y1="100" x2="290" y2="100" marker-end="url(#cr-ah)" />
        <text class="lbl" x="261" y="90" text-anchor="middle">HTTP</text>
        <line x1="490" y1="92" x2="548" y2="92" marker-end="url(#cr-ah)" />
        <line x1="548" y1="112" x2="490" y2="112" marker-end="url(#cr-ah)" />
        <text class="lbl" x="519" y="82" text-anchor="middle">gRPC</text>
        <line x1="390" y1="196" x2="390" y2="276" marker-end="url(#cr-ah)" />
        <text class="lbl" x="400" y="240">records</text>
      </g>
      <g class="dots" aria-hidden="true">
        {#each dots as d}
          <circle
            class:small={d.small}
            cx={d.path[0][0] + (d.path[1][0] - d.path[0][0]) * d.f}
            cy={d.path[0][1] + (d.path[1][1] - d.path[0][1]) * d.f}
            r={d.small ? 2.5 : 4}
          />
        {/each}
      </g>
    </svg>
  </div>

  <div class="part trace">
    <header>
      <span>Trace</span><small>records exported by the control plane</small>
    </header>
    <div class="rows" style:--f={position / duration}>
      {#each traceRows as [label, pick] (label)}
        <div class="trow">
          <span class="label">{label}</span>
          <span class="track">
            {#each events as e, i (e.seq)}
              {#if pick(e)}
                <i
                  class:seen={i < seen}
                  class:cur={i === seen - 1}
                  style:left={pct(e.at)}
                ></i>
              {/if}
            {/each}
          </span>
        </div>
      {/each}
      <b class="playhead"></b>
    </div>
  </div>

  <div class="part check">
    <header>
      <span>Compliance</span><small
        >do record sequences conform to our TLA+ spec</small
      >
      <span class="verdict" role="status"
        >{#if complete}verdict: <strong data-verdict={recording.suiteVerdict}
            >{recording.suiteVerdict}</strong
          >{/if}</span
      >
    </header>
    <div class="cells">
      {#each shown as inv (inv.key)}
        {@const [verdict, coverage] = verdicts[inv.key]}
        {@const state = verdict === "vacuous" ? "waiting" : verdict}
        <div class="cell" data-verdict={state}>
          <span class="mark" aria-hidden="true"></span>
          <span class="n">{inv.name}</span>
          <span class="v"
            >{state === "pass" ? `${coverage} checked` : state}</span
          >
        </div>
      {/each}
    </div>
  </div>
</section>

<style>
  .replay {
    scroll-margin-top: 1rem;
    /* The vivid tier tones the rest of the site charts with: blue for what
       moves, green for done and passing, amber for a destroy under way. */
    --move: var(--tone-gpu);
    --done: var(--tone-ram);
    --warm: var(--replay-warm);
    --bad: var(--tone-disk);
    --tint-accent: color-mix(in srgb, var(--move) 16%, var(--sheet));
    --tint-ok: color-mix(in srgb, var(--done) 18%, var(--sheet));
    --tint-warm: color-mix(in srgb, var(--warm) 24%, var(--sheet));
    --label: 8.5rem;
    margin: 1.2rem -1rem 0;
    min-width: 0;
    color: var(--ink);
    font-family: var(--font-ui);
    border-top: 1px solid var(--stroke);
  }
  .cap {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 1rem;
    padding: 0.5rem 1rem;
    border-bottom: 1px solid var(--line);
    font: 0.7rem var(--font-code);
  }
  .cap b {
    font-weight: 600;
  }
  .controls {
    display: flex;
    align-items: center;
    gap: 0.6rem;
  }
  button {
    font: inherit;
    color: var(--ink);
    background: var(--sheet);
    border: 1px solid var(--stroke);
    padding: 0.3rem 0.6rem;
    border-radius: 2px;
    cursor: pointer;
  }
  .controls :global(button[data-pointer]:focus) {
    outline: none;
  }
  button:focus-visible,
  input:focus-visible {
    outline: 2px solid var(--accent-ink);
    outline-offset: 2px;
  }
  .scrub input {
    width: 9rem;
    margin: 0;
    accent-color: var(--move);
  }
  .topo {
    padding: 1rem 1rem 0.8rem;
    border-bottom: 1px solid var(--stroke);
    overflow-x: auto;
  }
  /* Capped by viewport height so the drawing, the trace and the rules fit
     one screen while the run plays. */
  .topo svg {
    display: block;
    width: 100%;
    min-width: 40rem;
    max-height: 46vh;
    height: auto;
    margin: 0 auto;
    color: var(--ink);
  }
  .pod rect {
    fill: var(--sheet);
    stroke: currentColor;
    stroke-width: 1.25;
  }
  .pod line {
    stroke: currentColor;
    stroke-width: 1.25;
  }
  .pod text {
    font-family: var(--font-code);
    font-size: 12px;
    fill: currentColor;
  }
  .pod .sub,
  .pod .row {
    fill: var(--ink-2);
  }
  .pod .t {
    font-weight: 600;
    letter-spacing: 0.03em;
  }
  .pod .big {
    font-size: 18px;
    font-weight: 600;
  }
  .edge line {
    stroke: currentColor;
    stroke-width: 1;
  }
  .edge .lbl {
    font-family: var(--font-code);
    font-size: 11px;
    fill: var(--ink-2);
  }
  .sc text {
    fill: var(--ink-3);
  }
  .sc .id {
    font-weight: 600;
  }
  .sc.on text,
  .sc.done text {
    fill: var(--ink);
  }
  .sc rect {
    fill: transparent;
    stroke: none;
    transition: fill 200ms ease;
  }
  .sc.on rect {
    fill: var(--tint-accent);
  }
  .sc line {
    stroke: var(--line);
    stroke-width: 1;
  }
  .sc .ok {
    fill: var(--done);
  }
  .slot rect {
    fill: var(--band);
    stroke: var(--ink-3);
    stroke-width: 1;
    stroke-dasharray: 4 3;
    transition:
      fill 240ms ease,
      stroke 240ms ease;
  }
  .slot text {
    font-size: 11.5px;
    fill: var(--ink-3);
  }
  .slot .st {
    font-size: 10.5px;
  }
  .slot[data-state] text {
    fill: var(--ink);
  }
  .slot[data-state] .st {
    fill: var(--ink-2);
  }
  .slot[data-state="primed"] rect {
    stroke: var(--ink-2);
    stroke-dasharray: none;
  }
  .slot[data-state="running"] rect {
    fill: var(--tint-accent);
    stroke: var(--move);
    stroke-width: 2;
    stroke-dasharray: none;
  }
  .slot[data-state="finished"] rect {
    fill: var(--tint-ok);
    stroke: var(--done);
    stroke-dasharray: none;
  }
  .slot[data-state="destroying"] rect {
    fill: var(--tint-warm);
    stroke: var(--warm);
    stroke-width: 2;
    stroke-dasharray: none;
  }
  .slot[data-state="destroyed"] rect {
    fill: transparent;
  }
  .slot[data-state="destroyed"] text {
    fill: var(--ink-3);
  }
  .dots circle {
    fill: var(--move);
  }
  .dots circle.small {
    fill: var(--ink-3);
  }
  .part header {
    display: flex;
    align-items: baseline;
    gap: 0.5rem;
    padding: 0.45rem 1rem;
    border-bottom: 1px solid var(--stroke);
    font: 0.72rem var(--font-code);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .part header small {
    text-transform: none;
    letter-spacing: 0;
    color: var(--ink-2);
  }
  .part header .verdict {
    margin-left: auto;
    text-transform: none;
    letter-spacing: 0;
  }
  .verdict strong[data-verdict="pass"] {
    color: var(--done);
  }
  .verdict strong[data-verdict="fail"] {
    color: var(--bad);
  }
  .trace {
    border-bottom: 1px solid var(--stroke);
  }
  .rows {
    position: relative;
    display: grid;
    gap: 0.3rem;
    padding: 0.6rem 1rem 0.5rem;
    font: 0.72rem var(--font-code);
  }
  .trow {
    display: grid;
    grid-template-columns: var(--label) minmax(0, 1fr);
    align-items: center;
    gap: 0.5rem;
    height: 1.25rem;
  }
  .trow .label {
    color: var(--ink-2);
    white-space: nowrap;
  }
  .trow .track {
    position: relative;
    display: block;
    height: 0.85rem;
    background: var(--band);
  }
  .trow .track i {
    position: absolute;
    top: 0;
    bottom: 0;
    width: 4px;
    margin-left: -2px;
    background: var(--move);
    opacity: 0.18;
  }
  .trow:nth-child(2) .track i {
    background: var(--ink-2);
  }
  .trow .track i.seen {
    opacity: 1;
  }
  .trow .track i.seen.cur {
    background: var(--move);
  }
  /* The playhead spans both tracks: the rows' inline padding and the label
     column are subtracted so it lines up with the marks. */
  .rows .playhead {
    position: absolute;
    top: 0.6rem;
    bottom: 0.5rem;
    left: calc(
      1rem + var(--label) + 0.5rem + (100% - 2rem - var(--label) - 0.5rem) *
        var(--f, 0)
    );
    width: 2px;
    margin-left: -1px;
    background: var(--move);
    pointer-events: none;
  }
  .cells {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 1px;
    background: var(--line);
  }
  .cell {
    display: grid;
    grid-template-columns: 1.3rem minmax(0, 1fr);
    grid-template-areas: "m n" "m v";
    gap: 0 0.55rem;
    align-items: center;
    min-height: 3.4rem;
    padding: 0.6rem 1rem;
    background: var(--sheet);
    font-size: 0.75rem;
  }
  .cells .mark {
    grid-area: m;
    position: relative;
    box-sizing: border-box;
    width: 1.1rem;
    height: 1.1rem;
    border: 1.5px dashed var(--ink-3);
    border-radius: 50%;
    transition:
      background-color 240ms ease,
      border-color 240ms ease;
  }
  .cells .n {
    grid-area: n;
    line-height: 1.2;
    overflow-wrap: anywhere;
  }
  .cells .v {
    grid-area: v;
    font: 0.65rem var(--font-code);
    color: var(--ink-3);
  }
  .cell[data-verdict="waiting"] .n {
    color: var(--ink-2);
  }
  .cell[data-verdict="pass"] .mark {
    border: 0;
    background: var(--done);
  }
  .cell[data-verdict="pass"] .mark::after {
    content: "";
    position: absolute;
    left: 0.32rem;
    top: 0.17rem;
    width: 0.3rem;
    height: 0.55rem;
    border: solid var(--sheet);
    border-width: 0 2px 2px 0;
    transform: rotate(45deg);
  }
  .cell[data-verdict="pass"] .v {
    color: var(--done);
  }
  .cell[data-verdict="fail"] .mark {
    border: 0;
    background: var(--bad);
  }
  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
  }
  @media (max-width: 640px) {
    .replay {
      --label: 6rem;
    }
    .cap {
      flex-wrap: wrap;
    }
    .scrub input {
      width: 7rem;
    }
    .cells {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
  }
  @media (prefers-reduced-motion: reduce) {
    .sc rect,
    .slot rect,
    .cells .mark {
      transition: none;
    }
  }
</style>
