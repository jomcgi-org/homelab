<script>
  import { onMount } from "svelte";
  import { servingWakeMs } from "$lib/public/fcstory/metrics.js";
  import EmberSheet from "$lib/public/ember/EmberSheet.svelte";

  // Headline numbers count up from zero on load; skipped under reduced
  // motion (which also keeps visual-regression captures deterministic).
  let bestWake = $state(servingWakeMs);
  let vmRestore = $state(22);

  onMount(() => {
    const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (!reduced) {
      const t0 = performance.now();
      const dur = 900;
      bestWake = 0;
      vmRestore = 0;
      const tick = (t) => {
        const p = Math.min(1, (t - t0) / dur);
        const ease = 1 - Math.pow(1 - p, 3);
        bestWake = Math.round(servingWakeMs * ease);
        vmRestore = Math.round(22 * ease);
        if (p < 1) requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
    }
  });
</script>

<svelte:head>
  <title>Ember · a workload orchestrator on Firecracker microVMs</title>
  <meta
    name="description"
    content="Ember gives every job its own tiny virtual machine: run once and destroyed, slept as a snapshot and woken in {servingWakeMs} ms, or paired with a disk that outlives the VM. Includes recorded VM restores."
  />
</svelte:head>

<EmberSheet sections={[{ id: "classes", label: "What it runs" }]}>
  <header class="edition ed-lead masthead">
    <h1><span class="word">Ember</span></h1>
    <p class="lede">
      Every job gets its own tiny virtual machine. Idle ones sleep as snapshots; <b
        >disk to answering queries in 78&nbsp;ms</b
      >. Built from scratch on this cluster.
    </p>
    <p class="live">
      <a href="/ember/firecracker#replay">Replay a recorded VM restore</a>
    </p>
    <p class="stats">
      <span><b>{bestWake} ms</b> best wake</span>
      <span class="sep">·</span>
      <span><b>~{vmRestore} ms</b> VM restore</span>
      <span class="sep">·</span>
      <span>recorded runs</span>
      <span class="sep">·</span>
      <a
        class="src"
        href="https://github.com/jomcgi/homelab/blob/main/projects/embervm/ARCHITECTURE.md"
        >source</a
      >
    </p>
    <p class="ctas">
      <a class="cta cta-primary" href="/ember/firecracker#replay"
        >watch a replay</a
      >
      <a
        class="cta"
        href="https://github.com/jomcgi/homelab/blob/main/projects/embervm/ARCHITECTURE.md"
        >read the docs</a
      >
    </p>
  </header>

  <section class="edition">
    <h2 class="ed-head" id="classes">
      <a class="anchor" href="#classes">What it runs</a>
    </h2>
    <div class="classes">
      <details class="class" name="em-classes">
        <summary>
          <span class="cname">task</span>
          <span class="ctag">run once</span>
        </summary>
        <div class="cmore">
          <p>
            A fresh VM per job, <b>no network device</b>, destroyed after. The
            guest reaches exactly one thing: its channel to the host daemon.
          </p>
          <p class="sigs">
            <a class="sig" href="/ember/firecracker#replay"
              >replay a VM restore →</a
            >
          </p>
        </div>
      </details>
      <details class="class" name="em-classes">
        <summary>
          <span class="cname">session</span>
          <span class="ctag">sleep &amp; wake</span>
        </summary>
        <div class="cmore">
          <p>
            An agent's sandbox. <b>Banked</b> between turns, <b>relit</b> with memory,
            processes and open files intact; snapshots offload to S3, so a session
            survives its node.
          </p>
          <p class="sigs">
            <a class="sig" href="/ember/agents">one microVM per agent →</a>
          </p>
        </div>
      </details>
      <details class="class" name="em-classes">
        <summary>
          <span class="cname">serving</span>
          <span class="ctag">always answering</span>
        </summary>
        <div class="cmore">
          <p>
            A warm HTTP endpoint. Requests go through a node-local Envoy
            straight into the VM; <b
              >the control plane can restart mid-request</b
            > and traffic notices nothing.
          </p>
        </div>
      </details>
      <details class="class" name="em-classes">
        <summary>
          <span class="cname">stateful</span>
          <span class="ctag">a database that sleeps</span>
        </summary>
        <div class="cmore">
          <p>
            Postgres banked to disk when idle, woken by the next connection. <b
              >Zero compute while asleep</b
            >, and the disk outlives the VM.
          </p>
          <p class="sigs">
            <a class="sig" href="/ember/firecracker#replay"
              >replay a VM restore →</a
            >
          </p>
        </div>
      </details>
      <details class="class" name="em-classes">
        <summary>
          <span class="cname">composite</span>
          <span class="ctag">wakes as one</span>
        </summary>
        <div class="cmore">
          <p>
            Several VMs, one private network, banked and relit as a unit. A
            scratch Kubernetes cluster ran as one workload.
          </p>
        </div>
      </details>
      <details class="class" name="em-classes">
        <summary>
          <span class="cname">isolation</span>
          <span class="ctag">hostile by default</span>
        </summary>
        <div class="cmore">
          <p>
            <b
              >No VM, and nothing it was restored from, is ever shared between
              two customers.</b
            > Task and session guests have no network device at all. Quotas fail closed:
            quota 0 is a hard stop at submit.
          </p>
        </div>
      </details>
      <details class="class" name="em-classes">
        <summary>
          <span class="cname">the moving parts</span>
          <span class="ctag">how it fits together</span>
        </summary>
        <div class="cmore">
          <p>
            An <b>Elixir control plane</b> manages Firecracker VM lifecycle on Kubernetes;
            a Go daemon on each node owns the machines. Workloads are declared as
            Kubernetes custom resources.
          </p>
          <pre class="api">POST /v1/workloads/:name/tasks     → 202 + a task_id
POST /v1/workloads/:name/sessions  → a session_id, then /v1/sessions/:id/invoke
serving                            → plain HTTP, straight into the VM</pre>
          <!-- svelte-ignore a11y_no_noninteractive_tabindex (The labelled diagram region scrolls horizontally on phones.) -->
          <div
            class="arch"
            role="region"
            aria-label="Ember architecture diagram"
            tabindex="0"
          >
            <svg
              viewBox="0 0 720 300"
              role="img"
              aria-label="Ember architecture: callers reach the Elixir control plane, which dispatches over gRPC to the Go node daemon driving Firecracker VMs; serving traffic bypasses the control plane entirely via a node-local Envoy programmed over xDS. Snapshots and boot images are banked to S3."
            >
              <defs>
                <marker
                  id="arrow-ember"
                  markerWidth="8"
                  markerHeight="8"
                  refX="7"
                  refY="4"
                  orient="auto"
                >
                  <path class="mk mk-control" d="M1,1 L7,4 L1,7" />
                </marker>
                <marker
                  id="arrow-frost"
                  markerWidth="8"
                  markerHeight="8"
                  refX="7"
                  refY="4"
                  orient="auto"
                >
                  <path class="mk mk-data" d="M1,1 L7,4 L1,7" />
                </marker>
                <marker
                  id="arrow-amber"
                  markerWidth="8"
                  markerHeight="8"
                  refX="7"
                  refY="4"
                  orient="auto"
                >
                  <path class="mk mk-xds" d="M1,1 L7,4 L1,7" />
                </marker>
                <marker
                  id="arrow-slate"
                  markerWidth="8"
                  markerHeight="8"
                  refX="7"
                  refY="4"
                  orient="auto"
                >
                  <path class="mk mk-bank" d="M1,1 L7,4 L1,7" />
                </marker>
              </defs>

              <rect class="lane" x="150" y="18" width="230" height="264" />
              <text x="162" y="38" class="lane-label"
                >CONTROL PLANE · ELIXIR/OTP</text
              >
              <rect class="lane" x="420" y="18" width="286" height="264" />
              <text x="432" y="38" class="lane-label"
                >EACH FIRECRACKER NODE</text
              >

              <rect x="14" y="74" width="100" height="44" class="box" />
              <text x="64" y="93" text-anchor="middle" class="node-label"
                >caller</text
              >
              <text x="64" y="107" text-anchor="middle" class="node-sub"
                >task / session</text
              >

              <rect x="14" y="196" width="100" height="44" class="box" />
              <text x="64" y="215" text-anchor="middle" class="node-label"
                >edge</text
              >
              <text x="64" y="229" text-anchor="middle" class="node-sub"
                >HTTPRoute</text
              >

              <rect x="170" y="66" width="120" height="46" class="box" />
              <text x="230" y="86" text-anchor="middle" class="node-label"
                >HTTP API</text
              >
              <text x="230" y="101" text-anchor="middle" class="node-sub"
                >/v1/workloads</text
              >

              <rect x="170" y="130" width="120" height="42" class="box" />
              <text x="230" y="149" text-anchor="middle" class="node-label"
                >op-log</text
              >
              <text x="230" y="163" text-anchor="middle" class="node-sub"
                >Postgres</text
              >

              <rect x="170" y="192" width="120" height="42" class="box" />
              <text x="230" y="211" text-anchor="middle" class="node-label"
                >xDS publisher</text
              >
              <text x="230" y="225" text-anchor="middle" class="node-sub"
                >programs Envoy</text
              >

              <rect x="436" y="60" width="130" height="46" class="box" />
              <text x="501" y="80" text-anchor="middle" class="node-label"
                >noded</text
              >
              <text x="501" y="95" text-anchor="middle" class="node-sub"
                >Go daemon</text
              >

              <rect x="622" y="60" width="72" height="46" class="box" />
              <text x="658" y="80" text-anchor="middle" class="node-label"
                >VM</text
              >
              <text x="658" y="95" text-anchor="middle" class="node-sub"
                >vsock only</text
              >

              <rect x="436" y="136" width="130" height="40" class="box" />
              <text x="501" y="154" text-anchor="middle" class="node-label"
                >S3</text
              >
              <text x="501" y="168" text-anchor="middle" class="node-sub"
                >snapshots + images</text
              >
              <path class="path-bank" d="M494,106 L494,132" />
              <path class="path-bank" d="M508,132 L508,106" />
              <text
                x="548"
                y="124"
                text-anchor="middle"
                class="edge-label el-bank">bank</text
              >

              <rect x="436" y="196" width="130" height="46" class="box" />
              <text x="501" y="216" text-anchor="middle" class="node-label"
                >node Envoy</text
              >
              <text x="501" y="231" text-anchor="middle" class="node-sub"
                >exact-match</text
              >

              <rect x="622" y="196" width="72" height="46" class="box" />
              <text x="658" y="216" text-anchor="middle" class="node-label"
                >VM</text
              >
              <text x="658" y="231" text-anchor="middle" class="node-sub"
                >tap NIC</text
              >

              <path class="path-control" d="M114,92 L166,90" />
              <path class="path-control" d="M290,86 L432,83" />
              <text
                x="360"
                y="75"
                text-anchor="middle"
                class="edge-label el-control">gRPC</text
              >
              <path class="path-control" d="M566,83 L618,83" />
              <text
                x="592"
                y="76"
                text-anchor="middle"
                class="edge-label el-control">vsock</text
              >

              <path
                class="path-data"
                d="M114,222 C 140,222 138,262 170,262 L 350,262 C 400,262 396,222 432,222"
              />
              <text
                x="260"
                y="277"
                text-anchor="middle"
                class="edge-label el-data">bypasses the control plane</text
              >
              <path class="path-data" d="M566,219 L618,219" />
              <text
                x="592"
                y="212"
                text-anchor="middle"
                class="edge-label el-data">DNAT</text
              >

              <path class="path-xds" d="M290,213 C 340,213 380,208 432,207" />
              <text
                x="360"
                y="199"
                text-anchor="middle"
                class="edge-label el-xds">xDS</text
              >
            </svg>
            <div class="legend">
              <span
                ><i class="swatch sw-control"></i> control path (tasks &amp; sessions)</span
              >
              <span><i class="swatch sw-data"></i> serving data path</span>
              <span
                ><i class="swatch sw-xds"></i> configuration, ahead of time</span
              >
            </div>
          </div>
          <p class="sigs">
            <a class="sig" href="/ember/firecracker"
              >how a VM resumes in 22 ms →</a
            >
            <a
              class="sig"
              href="https://github.com/jomcgi/homelab/blob/main/projects/embervm/ARCHITECTURE.md"
              >full architecture →</a
            >
          </p>
        </div>
      </details>
    </div>
  </section>

  <footer class="edition ed-lead foot">
    <span
      >Elixir/OTP control plane · Go node daemon · Firecracker microVMs · 10 of
      12 roadmap milestones shipped</span
    >
    <span class="foot-links">
      <a href="https://github.com/jomcgi/homelab/tree/main/projects/embervm"
        >github.com/jomcgi/homelab</a
      >
      <a href="/">jomcgi.dev</a>
    </span>
  </footer>
</EmberSheet>

<style>
  .masthead .lede {
    max-width: 60ch;
  }
  .stats,
  .ctas,
  .foot-links,
  .sigs,
  .legend {
    display: flex;
    flex-wrap: wrap;
    gap: 0.6rem 1rem;
  }
  .stats,
  .ctas,
  .foot,
  .sigs,
  .legend {
    font-family: var(--font-code);
    font-size: 0.8rem;
  }
  .ctas a,
  .sig {
    text-underline-offset: 0.2em;
  }
  .class + .class {
    border-top: 1px solid var(--stroke);
  }
  .class summary {
    display: grid;
    grid-template-columns: minmax(8em, 1fr) minmax(0, 2fr) auto;
    align-items: baseline;
    gap: 0.5rem 1rem;
    padding: 1rem;
    cursor: pointer;
    list-style: none;
  }
  .class summary::-webkit-details-marker {
    display: none;
  }
  .class summary::after {
    content: "+";
  }
  .class[open] summary::after {
    content: "−";
  }
  .cname {
    font-weight: 600;
  }
  .ctag {
    color: var(--ink-2);
    font-size: 0.85rem;
  }
  .cmore {
    padding: 0 1rem 1rem;
  }
  .cmore p {
    margin: 0 0 1rem;
  }
  .cmore .sigs {
    margin: 1rem 0 0;
  }
  .api {
    padding: 1rem;
    border: 1px solid var(--stroke);
    background: var(--band);
    color: var(--ink);
    font-family: var(--font-code);
    font-size: 0.8rem;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  /* Keep the drawing legible on phones in its own keyboard-scrollable region. */
  .arch {
    overflow-x: auto;
    border: 1px solid var(--stroke);
    padding: 1rem;
  }
  .arch svg {
    display: block;
    width: 100%;
    min-width: 620px;
    height: auto;
  }
  .arch .lane {
    fill: none;
    stroke: var(--stroke);
    stroke-dasharray: 3 4;
  }
  .arch .box {
    fill: var(--sheet);
    stroke: var(--stroke);
  }
  .arch .lane-label,
  .arch .node-sub,
  .arch .edge-label {
    font-family: var(--font-code);
    font-size: 10.5px;
    fill: var(--ink-2);
  }
  .arch .node-label {
    font-family: var(--font-ui);
    font-size: 13px;
    font-weight: 600;
    fill: var(--ink);
  }
  .arch .mk,
  .arch .path-control,
  .arch .path-data,
  .arch .path-xds,
  .arch .path-bank {
    fill: none;
    stroke-width: 1;
    stroke: var(--ink);
  }
  .arch .path-control {
    marker-end: url(#arrow-ember);
    stroke: var(--accent-ink);
  }
  .arch .mk-control {
    stroke: var(--accent-ink);
  }
  .arch .path-data {
    marker-end: url(#arrow-frost);
  }
  .arch .path-xds {
    marker-end: url(#arrow-amber);
    stroke-dasharray: 5 4;
  }
  .arch .path-bank {
    marker-end: url(#arrow-slate);
    stroke-dasharray: 4 4;
  }
  .legend {
    margin-top: 1rem;
  }
  .legend span {
    display: inline-flex;
    align-items: center;
    gap: 0.5rem;
  }
  .swatch {
    width: 18px;
    border-top: 1px solid var(--ink);
  }
  .sw-control {
    border-color: var(--accent-ink);
  }
  .sw-xds {
    border-top-style: dashed;
  }
  .foot {
    overflow-wrap: anywhere;
  }
  .foot-links {
    margin-top: 1rem;
  }
  @media (max-width: 640px) {
    .class summary {
      grid-template-columns: minmax(0, 1fr) auto;
    }
    .ctag {
      grid-column: 1;
    }
    .class summary::after {
      grid-column: 2;
      grid-row: 1;
    }
  }
</style>
