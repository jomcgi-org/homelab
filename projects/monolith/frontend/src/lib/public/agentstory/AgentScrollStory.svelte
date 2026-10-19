<script>
  import TechnicalDrawingChrome from "../components/TechnicalDrawingChrome.svelte";
  import Trail from "../components/Trail.svelte";
  import SiteIndex from "../components/SiteIndex.svelte";
  import Diagram from "../components/diagrams/Diagram.svelte";
  import DBox from "../components/diagrams/DBox.svelte";
  import DGroup from "../components/diagrams/DGroup.svelte";
  import DArrow from "../components/diagrams/DArrow.svelte";
  import "../styles/technical-drawing.css";
  import "./agentstory.css";
  import { CALLS, CHAT_SCHEDULE, PHASES } from "./timeline.js";
  import { sessionRestoreMs } from "$lib/public/fcstory/metrics.js";

  // The original scroll timeline remains the content order. Normal flow keeps
  // every call and message reachable in SSR, reduced motion and short viewports.
  // No viewport measurement, animation frame or body style belongs to this page.
  const phases = [
    {
      id: "wake",
      title: `01 · wake: vm awake · ${sessionRestoreMs} ms, one shared base snapshot, volume patched while paused`,
      status: `off · restoring · awake · ${sessionRestoreMs} ms`,
      caption: "lifecycle",
    },
    {
      id: "hydrate",
      title: "02 · hydrate: clone jomcgi/homelab through the egress sidecar",
      status: "awake · turn running",
      caption: "data in flight",
    },
    {
      id: "creds",
      title: "03 · creds: real credentials stay outside the vm",
      status: "awake",
      caption: "credentialed egress",
    },
    {
      id: "park",
      title: "04 · park: idle 20 s, vm destroyed, disk kept",
      status: "idle 20 s… · asleep · parked",
      caption: "node scratch · nvme",
    },
    {
      id: "resume",
      title: "05 · resume: workspace in s3, restored on new node",
      status: "expired · 410",
      caption: "durable, to and from s3",
    },
  ];
  const messages = [
    "did last night's chart bump deploy?",
    `vm awake · ${sessionRestoreMs} ms`,
    "clone jomcgi/homelab · full history",
    "Checking ArgoCD and the chart versions.",
    "creds attached outside the vm",
    "Yes. The chart moved and the rollout is healthy.",
    "idle 20 s · vm destroyed · disk kept",
    "+2 days · workspace in s3",
    "add a regression test for that fix",
    "restored on new node · --resume",
    "Picking up where we left off.",
  ];
</script>

{#snippet trail()}
  <Trail crumbs={[{ label: "ember", href: "/ember" }, { label: "agents" }]} />
{/snippet}

<TechnicalDrawingChrome {trail} />
<main class="td sheet agent-sheet" aria-describedby="agents-architecture">
  <p class="sr-only" id="agents-architecture">
    Agent session architecture: the control plane places a session on a
    Firecracker node, the VM restores from a shared base snapshot with the
    session's workspace volume patched in, all egress including the git clone
    leaves through a credential-holding sidecar outside the VM, and parked or
    expired workspaces move between node scratch and SeaweedFS S3.
  </p>
  <div class="frame">
    <div class="journal">
      <aside class="spine">
        <div class="trail-dock">{@render trail()}</div>
        <nav class="index strip" aria-label="Agent session phases">
          <p class="sec-label">/ Index</p>
          <div class="index-list">
            {#each phases as phase, i (phase.id)}
              <a href={`#${phase.id}`}
                ><span class="num">0{i + 1}</span><span>{phase.id}</span></a
              >
            {/each}
            <a href="#tiers">Where a session lives</a>
            <a href="#keep-going">Keep going</a>
          </div>
        </nav>
        <SiteIndex here="engineering" />
      </aside>
      <div class="content editions">
        <section class="edition" aria-labelledby="agents-title">
          <div class="ed-head">agent session</div>
          <div class="ed-lead">
            <h1 id="agents-title">One microVM per agent session.</h1>
            <p>Killed on idle, rebuilt from disk or S3 on resume.</p>
            <p>Credentials stay <b>outside</b> the VM.</p>
          </div>
          <ul class="divided" aria-label="Session metrics">
            <li><b>2.5 ms</b> VM resume</li>
            <li><b>20 s</b> idle before the VM is destroyed</li>
            <li><b>0</b> real credentials inside</li>
          </ul>
          <div class="ed-foot"><a href="#wake">scroll ▼</a></div>
        </section>

        {#each phases as phase (phase.id)}
          {@const [heading, summary] = phase.title.split(": ")}
          <section
            class="edition agent-phase"
            id={phase.id}
            aria-labelledby={`${phase.id}-title`}
            data-phase={phase.id}
            data-start={PHASES[phase.id][0]}
            data-end={PHASES[phase.id][1]}
          >
            <div class="ed-head"><span>{phase.status}</span></div>
            <div class="ed-lead">
              <h2 id={`${phase.id}-title`}>{heading}:</h2>
              <p>{summary}</p>
            </div>
            <div class="ed-body prose">
              {#if phase.id === "wake"}
                <Diagram
                  label="lifecycle: control plane, gRPC, noded, session vm"
                >
                  <DBox sub="elixir/otp">control plane</DBox>
                  <DArrow label="gRPC" />
                  <DGroup label="brick-2 · firecracker node" stack>
                    <DBox sub="go daemon">noded</DBox>
                    <DBox sub="no network card · one channel to the host"
                      >session vm</DBox
                    >
                  </DGroup>
                </Diagram>
                <Diagram
                  label="node scratch: load the shared base memfile, patch workspace.img"
                >
                  <DGroup label="node scratch · nvme" stack>
                    <DBox sub="load · {sessionRestoreMs} ms"
                      >base memfile · shared</DBox
                    >
                    <DBox sub="patch">workspace.img · yours</DBox>
                  </DGroup>
                  <DArrow />
                  <DBox sub="vm off · vm awake">session vm</DBox>
                </Diagram>
              {:else if phase.id === "hydrate"}
                <Diagram
                  label="data in flight: session vm, vsock, egress sidecar, github.com clone + push, token attached"
                >
                  <DBox sub="vsock">session vm</DBox>
                  <DArrow />
                  <DBox sub="clone · token attached">egress sidecar</DBox>
                  <DArrow />
                  <DBox role="external" sub="clone + push">github.com</DBox>
                </Diagram>
              {:else if phase.id === "creds"}
                <Diagram
                  label="credentialed egress: egress sidecar holds the real secrets, swaps the placeholder header, TLS ends at sidecar"
                >
                  <DGroup label="egress sidecar" stack>
                    <DBox sub="holds the real secrets"
                      >Bearer &lt;placeholder&gt;</DBox
                    >
                    <DBox sub="swap in the real header"
                      >Bearer ●●●●●●●● · real</DBox
                    >
                  </DGroup>
                  <DArrow />
                  <DBox role="external" sub="tls ends at sidecar"
                    >api.anthropic.com</DBox
                  >
                </Diagram>
              {:else if phase.id === "park"}
                <Diagram
                  label="park: vm asleep, workspace.img kept on node scratch"
                >
                  <DBox sub="vm asleep">session vm</DBox>
                  <DArrow />
                  <DGroup label="node scratch · nvme" stack>
                    <DBox>base memfile · shared</DBox>
                    <DBox>workspace.img · yours</DBox>
                  </DGroup>
                </Diagram>
              {:else}
                <Diagram
                  label="durable, to and from s3: retire, export to object store, restore fresh vm on new node with --resume intact"
                >
                  <DGroup label="object store" stack>
                    <DBox>base/ snapshots</DBox>
                    <DBox sub="deleted after 7 days"
                      >session-workspace/&lt;lineage&gt;</DBox
                    >
                  </DGroup>
                  <DArrow label="restore" />
                  <DGroup label="new node" stack
                    ><DBox sub="--resume · intact">fresh vm</DBox></DGroup
                  >
                </Diagram>
              {/if}
            </div>
            <dl class="spec">
              <div class="row">
                <dt>{phase.caption}</dt>
                <dd>
                  {phase.id === "resume" ? "retire → export" : phase.status}
                </dd>
              </div>
              <div class="row">
                <dt>agent session</dt>
                <dd>
                  <ol class="agent-messages">
                    {#each CHAT_SCHEDULE as [at, id], i}
                      {#if id === phase.id}<li data-at={at}>
                          {messages[i]}
                        </li>{/if}
                    {/each}
                  </ol>
                </dd>
              </div>
              <div class="row">
                <dt>wire</dt>
                <dd>
                  <ol class="agent-calls">
                    {#each CALLS[phase.id] as call}<li
                        data-start={call.a}
                        data-end={call.b}
                      >
                        <code>{call.text}</code>
                      </li>{/each}
                  </ol>
                </dd>
              </div>
            </dl>
          </section>
        {/each}

        <section class="edition" id="tiers" aria-labelledby="tiers-title">
          <div class="ed-lead">
            <h2 id="tiers-title">Where a session lives</h2>
          </div>
          <dl class="spec">
            <div class="row">
              <dt>guest ram</dt>
              <dd>
                disposable · rebuilt in <b>{sessionRestoreMs} ms</b> · never snapshotted
              </dd>
            </div>
            <div class="row">
              <dt>node scratch</dt>
              <dd>
                shared base memfiles + <b>workspace.img</b> · what
                <b>--resume</b> reads
              </dd>
            </div>
            <div class="row">
              <dt>durable storage</dt>
              <dd>
                retired workspaces · <b>survives the machine</b> · deleted after 7
                days
              </dd>
            </div>
          </dl>
        </section>
        <section class="edition" id="keep-going" aria-labelledby="keep-title">
          <div class="ed-lead"><h2 id="keep-title">Keep going</h2></div>
          <nav aria-label="Keep going">
            <a class="agent-door" href="/ember/firecracker">
              <span>explainer</span>
              <h3>How Firecracker resumes a VM</h3>
              <p>
                What a microVM snapshot actually contains, and how a full
                machine comes back in ~22 ms.
              </p>
              <span>ember/firecracker →</span>
            </a>
            <a class="agent-door" href="/ember">
              <span>the mini-site</span>
              <h3>Ember</h3>
              <p>
                The workload orchestrator all of this runs on, with live demos
                you can wake yourself.
              </p>
              <span>ember →</span>
            </a>
          </nav>
          <footer class="ed-foot">
            <span
              >Elixir/OTP control plane · Go node daemon · Firecracker microVMs
              · running on this cluster</span
            ><span>jomcgi.dev</span>
          </footer>
        </section>
      </div>
    </div>
  </div>
</main>
