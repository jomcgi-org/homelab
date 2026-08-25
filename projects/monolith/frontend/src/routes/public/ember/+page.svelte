<script>
  // /ember: the landing page for the Ember mini-site. Narrative order is
  // what is it (masthead) -> is it useful to me (class rows) -> how does it
  // do that (diagram + isolation) -> see it run (demo doors); depth sits
  // behind accordion rows so the visible words stay few. Copy voice follows
  // the project README (blunt mechanism sentences, no rhetoric). Visual language is the shared ember token
  // sheet (lib/public/ember/ember.css) plus landing-only tokens in
  // ./landing.css; hex never appears in this <style> block.
  //
  // The status dot is read-only: SSR seeds it, a slow poll keeps it honest,
  // and the wake action stays on /ember/postgres where its Turnstile gate
  // and rate limits live.
  import { onMount } from "svelte";
  import { servingWakeMs } from "$lib/public/fcstory/metrics.js";
  import "$lib/public/ember/ember.css";
  import "./landing.css";

  let { data } = $props();

  // Same status endpoint the Postgres exhibit polls (cached control-plane
  // read; cannot wake the VM). Slow cadence: the landing page only needs
  // the dot to be honest, so one read every 15s is plenty.
  const STATUS_URL = "/ember/postgres/api/status";
  const POLL_MS = 15_000;

  // Same vocabulary as EmberStage's STATE_WORD so the landing page and the
  // demo never disagree about what the VM is doing.
  const STATE_WORD = {
    banked: "asleep",
    checkpointed: "asleep",
    banking: "falling asleep",
    relighting: "waking",
    cold_booting: "waking",
    starting: "waking",
    serving: "awake",
  };

  let status = $state(data.status);
  let stateWord = $derived(STATE_WORD[status?.state ?? ""] ?? null);
  let dotClass = $derived(
    stateWord === "awake"
      ? "live"
      : stateWord === "waking" || stateWord === "falling asleep"
        ? "waking"
        : "cold",
  );

  // Mirrors EmberStage.gbHours: raw MiB·s from the backend, shown as GB·h.
  function gbHours(mibSeconds) {
    if (typeof mibSeconds !== "number" || mibSeconds <= 0) return null;
    const gbh = mibSeconds / 1024 / 3600;
    if (gbh < 10) return `${gbh.toFixed(1)} GB·h`;
    if (gbh < 1000) return `${Math.round(gbh)} GB·h`;
    if (gbh < 1_000_000) return `${(gbh / 1000).toFixed(1)}K GB·h`;
    return `${(gbh / 1_000_000).toFixed(1)}M GB·h`;
  }

  let savedLine = $derived(
    gbHours(data.savings?.total_saved_mib_s ?? status?.total_saved_mib_s),
  );

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

    const poll = setInterval(async () => {
      if (document.hidden) return;
      try {
        const res = await fetch(STATUS_URL);
        if (res.ok) status = await res.json();
      } catch {
        // keep the last known state; the dot degrades gracefully
      }
    }, POLL_MS);
    return () => clearInterval(poll);
  });
</script>

<svelte:head>
  <title>Ember · a workload orchestrator on Firecracker microVMs</title>
  <meta
    name="description"
    content="Ember gives every job its own tiny virtual machine: run once and destroyed, slept as a snapshot and woken in {servingWakeMs} ms, or paired with a disk that outlives the VM. Includes a live Postgres you can wake yourself."
  />
</svelte:head>

<div class="ember-site">
  <header class="topbar">
    <span
      ><a class="brand" href="/"><strong>jomcgi.dev</strong></a> / ember</span
    >
    {#if savedLine}
      <span class="saved"
        >memory-hours not spent while asleep · <b>{savedLine}</b></span
      >
    {/if}
  </header>

  <main class="doc">
    <header class="masthead">
      <h1><span class="word">Ember</span></h1>
      <p class="lede">
        Ember gives every job its own tiny virtual machine. Some run once and
        are destroyed. Some sleep as snapshots and wake on demand,
        <b>disk to answering queries in 78&nbsp;ms</b>. Some keep a disk that
        outlives the VM. Built from scratch on this cluster, on
        <b>Firecracker</b>.
      </p>
      <p class="live">
        <span class="dot {dotClass}"></span>
        <span class="live-text">
          {#if stateWord === "awake"}
            the demo Postgres is <b>awake</b> right now.
            <a href="/ember/postgres">open the console</a>
          {:else if stateWord === "waking" || stateWord === "falling asleep"}
            the demo Postgres is <b>{stateWord}</b>.
            <a href="/ember/postgres">watch it on the live demo</a>
          {:else}
            the demo Postgres is <b>asleep</b> right now.
            <a href="/ember/postgres">wake it yourself on the live demo</a>
          {/if}
        </span>
      </p>
      <p class="stats">
        <span><b>{bestWake} ms</b> best wake</span>
        <span class="sep">·</span>
        <span><b>~{vmRestore} ms</b> VM restore</span>
        <span class="sep">·</span>
        <span>{stateWord ?? "asleep"} now</span>
        <span class="sep">·</span>
        <a
          class="src"
          href="https://github.com/jomcgi/homelab/blob/main/projects/embervm/ARCHITECTURE.md"
          >source</a
        >
      </p>
    </header>

    <section>
      <h2 class="h2" id="classes">
        <a class="anchor" href="#classes">What you'd run on it</a>
      </h2>
      <p class="body">
        Five classes, declared as Kubernetes custom resources, all assuming the
        guest is hostile.
      </p>
      <div class="classes">
        <details class="class" name="em-classes">
          <summary>
            <span class="cname">task<small>run once</small></span>
            <span class="cline"
              >A fresh VM, no network device, destroyed after one job.</span
            >
          </summary>
          <div class="cmore">
            <p>
              One-shot execution in a fresh or snapshot-restored VM. The guest
              can reach exactly one thing: its channel to the host daemon. The
              scan fleet runs the CI security scanner this way.
            </p>
          </div>
        </details>
        <details class="class" name="em-classes">
          <summary>
            <span class="cname">session<small>sleep &amp; wake</small></span>
            <span class="cline"
              >An agent's sandbox, banked between turns, relit with memory
              intact.</span
            >
          </summary>
          <div class="cmore">
            <p>
              Idle sessions are <b>banked</b> (snapshotted to disk) and
              <b>relit</b> on the next call with memory, processes and open
              files intact. Snapshots offload to S3, so a session survives the
              node it slept on. Each AI agent gets its own machine to make a
              mess in: shell, filesystem, packages,
              <b>destroyed without ceremony</b>.
            </p>
          </div>
        </details>
        <details class="class" name="em-classes">
          <summary>
            <span class="cname">serving<small>always answering</small></span>
            <span class="cline"
              >A warm HTTP endpoint; requests never touch the control plane.</span
            >
          </summary>
          <div class="cmore">
            <p>
              Requests reach the guest directly through a node-local Envoy the
              control plane has already programmed. An image renderer answers
              real internet traffic this way,
              <b>rate-limited and quota-capped</b>.
            </p>
          </div>
        </details>
        <details class="class" name="em-classes">
          <summary>
            <span class="cname"
              >stateful<small>a database that sleeps</small></span
            >
            <span class="cline"
              >Its disk outlives the VM; the next connection wakes it.</span
            >
          </summary>
          <div class="cmore">
            <p>
              Postgres banked to disk the moment it goes idle.
              <b>Zero compute while asleep</b>, and the volume on node NVMe is
              the authoritative copy.
              <a class="sig" href="/ember/postgres">see it live →</a>
            </p>
          </div>
        </details>
        <details class="class" name="em-classes">
          <summary>
            <span class="cname">composite<small>wakes as one</small></span>
            <span class="cline"
              >Several VMs, one private network, banked and relit together.</span
            >
          </summary>
          <div class="cmore">
            <p>
              An all-or-none group. A scratch Kubernetes cluster ran as one
              composite workload: control plane and workers woke together on the
              first kubectl.
            </p>
          </div>
        </details>
      </div>
      <pre class="api">POST /v1/workloads/:name/tasks     → 202 + a task_id
POST /v1/workloads/:name/sessions  → a session_id, then /v1/sessions/:id/invoke
serving                            → plain HTTP, straight into the VM</pre>
    </section>

    <section>
      <h2 class="h2" id="arch">
        <a class="anchor" href="#arch">How it works</a>
      </h2>
      <p class="body">
        An <b>Elixir control plane</b> manages Firecracker VM lifecycle on
        Kubernetes; a Go daemon on each node owns the machines.
        <b>Serving requests never touch the control plane</b>: the edge routes
        through a node-local Envoy straight into the VM, so the control plane
        can restart mid-request and traffic notices nothing.
      </p>
      <details class="fold">
        <summary><span class="fold-label">the moving parts</span></summary>
        <div class="arch">
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

            <rect
              class="lane"
              x="150"
              y="18"
              width="230"
              height="264"
              rx="10"
            />
            <text x="162" y="38" class="lane-label"
              >CONTROL PLANE · ELIXIR/OTP</text
            >
            <rect
              class="lane"
              x="420"
              y="18"
              width="286"
              height="264"
              rx="10"
            />
            <text x="432" y="38" class="lane-label">EACH FIRECRACKER NODE</text>

            <rect x="14" y="74" width="100" height="44" rx="8" class="box" />
            <text x="64" y="93" text-anchor="middle" class="node-label"
              >caller</text
            >
            <text x="64" y="107" text-anchor="middle" class="node-sub"
              >task / session</text
            >

            <rect x="14" y="196" width="100" height="44" rx="8" class="box" />
            <text x="64" y="215" text-anchor="middle" class="node-label"
              >edge</text
            >
            <text x="64" y="229" text-anchor="middle" class="node-sub"
              >HTTPRoute</text
            >

            <rect x="170" y="66" width="120" height="46" rx="8" class="box" />
            <text x="230" y="86" text-anchor="middle" class="node-label"
              >HTTP API</text
            >
            <text x="230" y="101" text-anchor="middle" class="node-sub"
              >/v1/workloads</text
            >

            <rect x="170" y="130" width="120" height="42" rx="8" class="box" />
            <text x="230" y="149" text-anchor="middle" class="node-label"
              >op-log</text
            >
            <text x="230" y="163" text-anchor="middle" class="node-sub"
              >Postgres</text
            >

            <rect x="170" y="192" width="120" height="42" rx="8" class="box" />
            <text x="230" y="211" text-anchor="middle" class="node-label"
              >xDS publisher</text
            >
            <text x="230" y="225" text-anchor="middle" class="node-sub"
              >programs Envoy</text
            >

            <rect x="436" y="60" width="130" height="46" rx="8" class="box" />
            <text x="501" y="80" text-anchor="middle" class="node-label"
              >noded</text
            >
            <text x="501" y="95" text-anchor="middle" class="node-sub"
              >Go daemon</text
            >

            <rect x="622" y="60" width="72" height="46" rx="8" class="box" />
            <text x="658" y="80" text-anchor="middle" class="node-label"
              >VM</text
            >
            <text x="658" y="95" text-anchor="middle" class="node-sub"
              >vsock only</text
            >

            <rect x="436" y="136" width="130" height="40" rx="8" class="box" />
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

            <rect x="436" y="196" width="130" height="46" rx="8" class="box" />
            <text x="501" y="216" text-anchor="middle" class="node-label"
              >node Envoy</text
            >
            <text x="501" y="231" text-anchor="middle" class="node-sub"
              >exact-match</text
            >

            <rect x="622" y="196" width="72" height="46" rx="8" class="box" />
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
            <text x="360" y="199" text-anchor="middle" class="edge-label el-xds"
              >xDS</text
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
      </details>
      <div class="iso">
        <p>
          <b
            >No VM, and nothing it was restored from, is ever shared between two
            customers.</b
          >
        </p>
        <p>
          Task and session guests have <b>no network device at all</b>: one
          channel to the host daemon.
        </p>
        <p>
          <b>Quotas fail closed.</b> A customer with quota 0 is hard-stopped at submit.
        </p>
      </div>
    </section>

    <section>
      <h2 class="h2" id="live-exhibits">
        <a class="anchor" href="#live-exhibits">See it run</a>
      </h2>
      <div class="doors">
        <a class="door" href="/ember/postgres">
          <span class="k">live demo</span>
          <h3>A Postgres that sleeps</h3>
          <p>Click connect, watch the stopwatch: best wake 78&nbsp;ms.</p>
          <span class="go">ember/postgres</span>
        </a>
        <a class="door" href="/ember/bazel">
          <span class="k">live demo</span>
          <h3>Query a frozen Bazel brain</h3>
          <p>
            Each query runs in a disposable clone of a snapshotted warm Bazel
            server.
          </p>
          <span class="go">ember/bazel</span>
        </a>
        <a class="door" href="/ember/firecracker">
          <span class="k">explainer</span>
          <h3>How Firecracker resumes a VM</h3>
          <p>What a snapshot contains; a full machine back in ~22&nbsp;ms.</p>
          <span class="go">ember/firecracker</span>
        </a>
        <a class="door" href="/ember/agents">
          <span class="k">explainer</span>
          <h3>One microVM per agent session</h3>
          <p>Restored in 2.5 ms, killed 20 s after the last turn.</p>
          <span class="go">ember/agents</span>
        </a>
        <a class="door" href="/ember/semgrep">
          <span class="k">workload demo</span>
          <h3>Semgrep</h3>
          <p>Warm in a microVM, scanning your snippet in about a second.</p>
          <span class="go">ember/semgrep</span>
        </a>
      </div>
    </section>

    <footer class="foot">
      <span
        >Elixir/OTP control plane · Go node daemon · 10 of 12 roadmap milestones
        shipped</span
      >
      <span class="foot-links">
        <a href="https://github.com/jomcgi/homelab/tree/main/projects/embervm"
          >github.com/jomcgi/homelab</a
        >
        <a href="/">jomcgi.dev</a>
      </span>
    </footer>
  </main>
</div>

<style>
  .ember-site {
    min-height: 100dvh;
  }

  /* ---------- topbar ---------- */
  .topbar {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 16px;
    padding: 14px 28px;
    font-family: var(--em-mono);
    font-size: 12.5px;
    color: var(--em-muted);
  }

  .topbar strong {
    color: var(--em-ink);
    font-weight: 600;
  }

  .topbar .brand {
    color: inherit;
    text-decoration: none;
    border-radius: 4px;
  }

  .topbar .brand:hover {
    text-decoration: underline;
    text-underline-offset: 3px;
  }

  .topbar .brand:focus-visible {
    outline: 2px solid var(--em-ember-deep);
    outline-offset: 3px;
  }

  .saved {
    color: var(--em-faint);
  }

  .saved b {
    color: var(--em-ember-deep);
    font-weight: 600;
  }

  /* ---------- document frame ---------- */
  .doc {
    max-width: 880px;
    margin: 0 auto;
    padding: 24px 24px 90px;
  }

  /* ---------- masthead ---------- */
  .masthead {
    padding: 56px 0 8px;
  }

  .masthead h1 {
    margin: 0 0 10px;
    font-size: clamp(44px, 7vw, 76px);
    font-weight: 850;
    letter-spacing: -0.035em;
    line-height: 0.95;
    color: var(--em-ink);
  }

  .masthead .word {
    color: var(--em-ember);
  }

  .lede {
    margin: 0;
    /* Capped measure: the doc column is 880px, too wide for comfortable
       reading at this size. Air is part of the say-less pass. */
    max-width: 30em;
    font-size: clamp(17px, 2.1vw, 21px);
    line-height: 1.5;
    color: var(--em-ink);
  }

  .lede b {
    font-weight: 650;
  }

  .live {
    margin: 18px 0 0;
    font-family: var(--em-mono);
    font-size: 13px;
    color: var(--em-muted);
    display: flex;
    align-items: center;
    gap: 9px;
  }

  .dot {
    width: 9px;
    height: 9px;
    border-radius: 50%;
    flex: none;
    transition:
      background 0.6s ease,
      box-shadow 0.6s ease;
  }

  .dot.cold {
    background: radial-gradient(
      circle at 35% 35%,
      var(--eml-dot-cold-hi),
      var(--em-frost) 70%,
      var(--eml-dot-cold-lo)
    );
    box-shadow: 0 0 7px 1px rgba(61, 126, 194, 0.4);
    animation: breathe-cold 4.2s ease-in-out infinite;
  }

  .dot.waking {
    background: radial-gradient(
      circle at 35% 35%,
      var(--eml-dot-wake-hi),
      var(--em-amber) 65%,
      var(--em-ember)
    );
    box-shadow: 0 0 10px 2px rgba(242, 176, 78, 0.55);
    animation: breathe-warm 0.9s ease-in-out infinite;
  }

  .dot.live {
    background: radial-gradient(
      circle at 35% 35%,
      var(--eml-dot-warm-hi),
      var(--em-ember) 65%,
      var(--em-ember-deep)
    );
    box-shadow: 0 0 8px 1px rgba(224, 66, 26, 0.55);
    animation: breathe-warm 3.2s ease-in-out infinite;
  }

  @keyframes breathe-cold {
    0%,
    100% {
      box-shadow: 0 0 4px 0 rgba(61, 126, 194, 0.25);
    }
    50% {
      box-shadow: 0 0 10px 2px rgba(61, 126, 194, 0.45);
    }
  }

  @keyframes breathe-warm {
    0%,
    100% {
      box-shadow: 0 0 5px 0 rgba(224, 66, 26, 0.35);
    }
    50% {
      box-shadow: 0 0 12px 3px rgba(224, 66, 26, 0.6);
    }
  }

  .live a {
    color: var(--em-ember-deep);
    text-decoration: none;
    border-bottom: 1px solid var(--em-ember-dim);
  }

  .live a:hover {
    border-bottom-color: var(--em-ember-deep);
  }

  .live a:focus-visible,
  .sig:focus-visible,
  .anchor:focus-visible,
  .door:focus-visible,
  .foot a:focus-visible {
    outline: 2px solid var(--em-ember-deep);
    outline-offset: 3px;
  }

  .stats {
    margin: 16px 0 0;
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 6px 12px;
    font-family: var(--em-mono);
    font-size: 12.5px;
    line-height: 1.6;
    color: var(--em-faint);
    font-variant-numeric: tabular-nums;
  }

  .stats b {
    color: var(--em-ember-deep);
    font-weight: 600;
  }

  .stats .sep {
    color: var(--eml-line-strong);
  }

  .stats .src {
    color: var(--em-ember-deep);
    text-decoration: none;
    border-bottom: 1px solid var(--em-ember-dim);
  }

  .stats .src:hover {
    border-bottom-color: var(--em-ember-deep);
  }

  .stats .src:focus-visible {
    outline: 2px solid var(--em-ember-deep);
    outline-offset: 3px;
  }

  /* ---------- section headings, README style ---------- */
  .h2 {
    display: flex;
    align-items: baseline;
    gap: 12px;
    margin: 64px 0 18px;
    font-size: 24px;
    font-weight: 750;
    letter-spacing: -0.015em;
    color: var(--em-ink);
  }

  .h2::before {
    content: "##";
    font-family: var(--em-mono);
    font-size: 16px;
    font-weight: 400;
    color: var(--em-ember);
    transform: translateY(-2px);
  }

  .anchor {
    color: inherit;
    text-decoration: none;
  }

  .body {
    margin: 0 0 14px;
    max-width: 46em;
    font-size: 15.5px;
    line-height: 1.55;
    color: var(--em-muted);
  }

  .body b {
    color: var(--em-ink);
    font-weight: 600;
  }

  .classes-after {
    margin: 14px 0 0;
  }

  /* The invocation surface, shown rather than described. Mono on the shared
     track tint; same soft-border language as the arch panel. */
  .api {
    margin: 16px 0 0;
    padding: 13px 16px;
    background: var(--em-track);
    border: 1px solid var(--em-line);
    border-radius: 8px;
    font-family: var(--em-mono);
    font-size: 12.5px;
    line-height: 1.8;
    color: var(--em-muted);
    overflow-x: auto;
  }

  .classes code {
    font-family: var(--em-mono);
    font-size: 0.88em;
    background: var(--em-track);
    border-radius: 4px;
    padding: 1px 5px;
    color: var(--em-ink);
  }

  /* ---------- ruled class rows: one line visible, detail on demand ------
     The picomq-style accordion, done as native <details name="em-classes">
     so one row is open at a time with no JS; older browsers just get
     independent toggles. The affordance is a mono plus that rotates to a
     multiply: rotation only, per the transform-and-opacity motion rule. */
  .classes {
    display: flex;
    flex-direction: column;
    border-top: 1px solid var(--eml-line-strong);
    margin: 0;
  }

  .class {
    border-bottom: 1px solid var(--em-line);
  }

  .class summary {
    display: grid;
    grid-template-columns: 210px minmax(0, 1fr) 18px;
    gap: 18px;
    align-items: baseline;
    padding: 14px 4px;
    cursor: pointer;
    list-style: none;
  }

  .class summary::-webkit-details-marker {
    display: none;
  }

  .cname {
    font-family: var(--em-mono);
    font-size: 14px;
    font-weight: 600;
    line-height: 1.45;
    color: var(--em-ember-deep);
  }

  .cname small {
    display: block;
    font-weight: 400;
    color: var(--em-faint);
    font-size: 11px;
    margin-top: 5px;
  }

  .cline {
    font-size: 15px;
    line-height: 1.55;
    color: var(--em-muted);
  }

  .class summary::after {
    content: "+";
    font-family: var(--em-mono);
    font-size: 15px;
    line-height: 1;
    color: var(--em-faint);
    justify-self: end;
    align-self: center;
  }

  .class[open] summary::after {
    transform: rotate(45deg);
  }

  .class summary:hover .cline {
    color: var(--em-ink);
  }

  .class summary:focus-visible {
    outline: 2px solid var(--em-ember-deep);
    outline-offset: 3px;
  }

  .cmore {
    padding: 0 26px 16px 228px;
  }

  .cmore p {
    margin: 0;
    font-size: 15px;
    line-height: 1.55;
    color: var(--em-muted);
  }

  .cmore b {
    color: var(--em-ink);
    font-weight: 600;
  }

  .sig {
    font-family: var(--em-mono);
    font-size: 12.5px;
    color: var(--em-ember-deep);
    text-decoration: none;
    border-bottom: 1px solid var(--em-ember-dim);
    margin-left: 6px;
    white-space: nowrap;
  }

  .sig:hover {
    border-bottom-color: var(--em-ember-deep);
  }

  /* ---------- diagram fold: same disclosure language as the class rows ---- */
  .fold {
    border-top: 1px solid var(--eml-line-strong);
    border-bottom: 1px solid var(--em-line);
    margin-bottom: 16px;
  }

  .fold summary {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 18px;
    align-items: center;
    padding: 12px 4px;
    cursor: pointer;
    list-style: none;
  }

  .fold summary::-webkit-details-marker {
    display: none;
  }

  .fold-label {
    font-family: var(--em-mono);
    font-size: 12.5px;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--em-faint);
  }

  .fold summary::after {
    content: "+";
    font-family: var(--em-mono);
    font-size: 15px;
    line-height: 1;
    color: var(--em-faint);
    justify-self: end;
  }

  .fold[open] summary::after {
    transform: rotate(45deg);
  }

  .fold summary:hover .fold-label {
    color: var(--em-muted);
  }

  .fold summary:focus-visible {
    outline: 2px solid var(--em-ember-deep);
    outline-offset: 3px;
  }

  .fold .arch {
    margin-bottom: 14px;
  }

  /* ---------- architecture ---------- */
  .arch {
    background: var(--eml-panel-warm);
    border: 1px solid var(--em-line);
    border-radius: 12px;
    box-shadow: var(--em-shadow-soft);
    padding: 22px 22px 16px;
    overflow-x: auto;
  }

  .arch svg {
    display: block;
    min-width: 620px;
    width: 100%;
    height: auto;
  }

  .arch .lane {
    fill: none;
    stroke: var(--eml-line-strong);
    stroke-dasharray: 3 4;
  }

  .arch .box {
    fill: var(--em-ground);
    stroke: var(--eml-line-strong);
  }

  .arch .lane-label {
    font-family: var(--em-mono);
    font-size: 11px;
    fill: var(--em-faint);
    letter-spacing: 0.06em;
  }

  .arch .node-label {
    font-family: var(--em-sans);
    font-size: 13px;
    font-weight: 600;
    fill: var(--em-ink);
  }

  .arch .node-sub {
    font-family: var(--em-mono);
    font-size: 10.5px;
    fill: var(--em-faint);
  }

  .arch .mk {
    fill: none;
    stroke-width: 1.6;
  }

  .arch .mk-control {
    stroke: var(--em-ember);
  }

  .arch .mk-data {
    stroke: var(--em-frost);
  }

  .arch .mk-xds {
    stroke: var(--em-amber);
  }

  .arch .mk-bank {
    stroke: var(--eml-line-strong);
  }

  .arch .path-control {
    stroke: var(--em-ember);
    stroke-width: 2;
    fill: none;
    marker-end: url(#arrow-ember);
  }

  .arch .path-data {
    stroke: var(--em-frost);
    stroke-width: 2;
    fill: none;
    marker-end: url(#arrow-frost);
  }

  .arch .path-xds {
    stroke: var(--em-amber);
    stroke-width: 1.6;
    stroke-dasharray: 5 4;
    fill: none;
    marker-end: url(#arrow-amber);
  }

  .arch .path-bank {
    stroke: var(--eml-line-strong);
    stroke-width: 1.6;
    stroke-dasharray: 4 4;
    fill: none;
    marker-end: url(#arrow-slate);
  }

  .arch .edge-label {
    font-family: var(--em-mono);
    font-size: 10.5px;
  }

  .arch .el-control {
    fill: var(--em-ember-deep);
  }

  .arch .el-data {
    fill: var(--em-frost);
  }

  .arch .el-xds {
    fill: var(--eml-amber-deep);
  }

  .arch .el-bank {
    fill: var(--em-faint);
  }

  .legend {
    display: flex;
    flex-wrap: wrap;
    gap: 18px;
    margin-top: 12px;
    font-family: var(--em-mono);
    font-size: 11.5px;
    color: var(--em-muted);
  }

  .legend span {
    display: inline-flex;
    align-items: center;
    gap: 7px;
  }

  .swatch {
    width: 18px;
    height: 0;
    border-top: 2px solid;
    display: inline-block;
  }

  .sw-control {
    border-color: var(--em-ember);
  }

  .sw-data {
    border-color: var(--em-frost);
  }

  .sw-xds {
    border-color: var(--em-amber);
    border-top-style: dashed;
  }

  /* ---------- isolation ---------- */
  .iso {
    display: flex;
    flex-direction: column;
    border-top: 1px solid var(--eml-line-strong);
  }

  .iso p {
    margin: 0;
    padding: 13px 4px;
    border-bottom: 1px solid var(--em-line);
    font-size: 16px;
    line-height: 1.55;
    color: var(--em-muted);
  }

  .iso p b {
    color: var(--em-ink);
    font-weight: 650;
  }

  /* ---------- doors ---------- */
  .doors {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 16px;
  }

  .door {
    display: block;
    text-decoration: none;
    background: var(--eml-panel-warm);
    border: 1px solid var(--em-line);
    border-radius: 12px;
    padding: 20px 22px 18px;
    box-shadow: var(--em-shadow-soft);
    transition:
      border-color 0.18s ease,
      box-shadow 0.18s ease,
      transform 0.18s ease;
  }

  .door:hover {
    border-color: var(--em-ember-dim);
    box-shadow: var(--em-shadow);
    transform: translateY(-2px);
  }

  .door .k {
    font-family: var(--em-mono);
    font-size: 11px;
    letter-spacing: 0.08em;
    color: var(--em-faint);
    text-transform: uppercase;
  }

  .door h3 {
    margin: 6px 0;
    font-size: 19px;
    font-weight: 700;
    color: var(--em-ink);
    letter-spacing: -0.01em;
  }

  .door:hover h3 {
    color: var(--em-ember-deep);
  }

  .door p {
    margin: 0;
    font-size: 14px;
    line-height: 1.5;
    color: var(--em-muted);
  }

  .door .go {
    display: inline-block;
    margin-top: 12px;
    font-family: var(--em-mono);
    font-size: 12.5px;
    color: var(--em-ember-deep);
  }

  .door .go::after {
    content: " →";
    transition: transform 0.18s ease;
    display: inline-block;
  }

  .door:hover .go::after {
    transform: translateX(4px);
  }

  /* Opening motion is opt-in: absent by default, per the reduced-motion
     opt-in shape this repo prefers for new work. */
  @media (prefers-reduced-motion: no-preference) {
    .class summary::after,
    .fold summary::after {
      transition: transform 0.18s ease;
    }

    .class[open] .cmore,
    .fold[open] .arch {
      animation: em-reveal 0.22s ease;
    }

    @keyframes em-reveal {
      from {
        opacity: 0;
        transform: translateY(-3px);
      }
    }
  }

  /* ---------- footer ---------- */
  .foot {
    margin-top: 70px;
    padding-top: 18px;
    border-top: 2px solid var(--em-ember-deep);
    display: flex;
    justify-content: space-between;
    gap: 16px;
    flex-wrap: wrap;
    font-family: var(--em-mono);
    font-size: 12.5px;
    color: var(--em-faint);
  }

  .foot-links {
    display: flex;
    gap: 18px;
  }

  .foot a {
    color: var(--em-muted);
    text-decoration: none;
    border-bottom: 1px solid var(--eml-line-strong);
  }

  .foot a:hover {
    color: var(--em-ember-deep);
    border-bottom-color: var(--em-ember-dim);
  }

  /* ---------- responsive ---------- */
  @media (max-width: 900px) {
    .topbar {
      padding: 12px 16px;
    }

    .doc {
      padding: 16px 16px 60px;
    }
  }

  @media (max-width: 700px) {
    .doors {
      grid-template-columns: 1fr;
    }
  }

  @media (max-width: 640px) {
    .class summary {
      grid-template-columns: minmax(0, 1fr) 18px;
      gap: 6px 14px;
    }

    .class summary .cline {
      grid-column: 1;
    }

    .class summary::after {
      grid-row: 1;
      grid-column: 2;
    }

    .cmore {
      padding-left: 4px;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .dot,
    .door,
    .door .go::after {
      transition: none;
    }

    .dot.cold,
    .dot.waking,
    .dot.live {
      animation: none;
    }
  }
</style>
