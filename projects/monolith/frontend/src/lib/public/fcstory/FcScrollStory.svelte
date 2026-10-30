<script>
  // Normal-flow panels keep every phase readable at phone widths and with
  // enlarged text. The former viewport-sized stage hid its phase key below
  // 1400px height and clipped long captions. There is now one SSR story for
  // all motion preferences, without mount-time layout changes or scroll work.
  import TechnicalDrawingChrome from "$lib/public/components/TechnicalDrawingChrome.svelte";
  import Trail from "$lib/public/components/Trail.svelte";
  import SiteIndex from "$lib/public/components/SiteIndex.svelte";
  import Diagram from "$lib/public/components/diagrams/Diagram.svelte";
  import DBox from "$lib/public/components/diagrams/DBox.svelte";
  import DGroup from "$lib/public/components/diagrams/DGroup.svelte";
  import DArrow from "$lib/public/components/diagrams/DArrow.svelte";
  import "$lib/public/styles/technical-drawing.css";
  import { coldSegments, restoreBarFraction } from "./timeline.js";
  import { cold, restores } from "./data/trace.js";
  import { sandboxRestoreMs } from "./metrics.js";
  import ReplayWidget from "./ReplayWidget.svelte";
  import "./fcstory.css";

  const PHASE_HUMAN = {
    auth_tokenreview: "verify the caller",
    acquire_slot: "take a slot",
    snapshot_restore: "disk → memory",
    guest_wait_ready: "guest ready",
    guest_exec: "run the code",
  };
  const coldBoot = cold.phases.find((p) => p.name === "firecracker_boot");
  const coldWait = cold.phases.find((p) => p.name === "guest_wait_ready");
  const coldSave = cold.phases.find((p) => p.name === "snapshot_save");
  const segs = coldSegments(cold);
  const firstRestore = restores[0];
  const restoreTotalMin = Math.min(...restores.map((r) => r.total));
  const restoreTotalMax = Math.max(...restores.map((r) => r.total));
  const sections = [
    { id: "cold", label: "Boot once" },
    { id: "freeze", label: "Freeze" },
    { id: "restore", label: "Restore" },
    { id: "runs", label: "Recorded runs" },
    { id: "replay", label: "Replay" },
  ];
</script>

{#snippet trail()}
  <Trail
    crumbs={[{ label: "ember", href: "/ember" }, { label: "firecracker" }]}
  />
{/snippet}

<TechnicalDrawingChrome {trail} />
<main class="td sheet fcstory">
  <div class="frame">
    <div class="journal">
      <aside class="spine">
        <div class="trail-dock">{@render trail()}</div>
        <nav class="index strip" aria-label="On this page">
          <p class="sec-label">/ Index</p>
          <div class="index-list">
            {#each sections as section, i (section.id)}
              <a href={`#${section.id}`}
                ><span class="num">{String(i + 1).padStart(2, "0")}</span
                >{section.label}</a
              >
            {/each}
          </div>
        </nav>
        <SiteIndex />
      </aside>
      <div class="content editions">
        <section class="edition" aria-labelledby="story-title">
          <div class="ed-head">
            <span>firecracker</span><a class="end" href="#replay"
              >Restore again</a
            >
          </div>
          <div class="ed-lead">
            <h1 id="story-title">Boot once. Restore forever.</h1>
            <p>
              This site runs untrusted code in Firecracker microVMs. Building
              the first one took <span class="num"
                >{(cold.total / 1000).toFixed(1)} seconds</span
              >. Every sandbox since has woken from disk in about
              <span class="num"
                >{Math.round(sandboxRestoreMs)} milliseconds</span
              >. Scroll to see how.
            </p>
          </div>
        </section>

        <section class="edition" id="cold" aria-labelledby="cold-title">
          <div class="ed-head">
            <span class="num">01</span><span
              >Cold, once at startup: <span class="num"
                >{cold.total.toLocaleString()} ms</span
              ></span
            >
          </div>
          <div class="ed-lead">
            <h2 id="cold-title">
              Firecracker boots a fresh microVM in <span class="num"
                >{coldBoot.ms.toFixed(0)}&nbsp;ms</span
              >.
            </h2>
            <p>
              The slow part is waiting for a useful guest: kernel up, agent
              listening, toolchain warm. Measured cold, that wait is
              <span class="num">{coldWait.ms.toLocaleString()}&nbsp;ms</span>.
            </p>
          </div>
          <div class="ed-body prose">
            <Diagram label="microVM · guest RAM">
              <DGroup label="A · kata-fc · node-local">
                <DBox sub="vcpu 1 · jailed">booting</DBox>
                <DBox>warming up</DBox>
                <DBox>ready</DBox>
              </DGroup>
            </Diagram>
            <div class="timing">
              <p>cold: build the base snapshot (once, at daemon startup)</p>
              <div class="timing-bar" aria-hidden="true">
                {#each segs as segment (segment.name)}
                  <span
                    style={`width: ${segment.fraction * 100}%; background: var(--fc-phase-${segment.name.replace(/_/g, "-")})`}
                  ></span>
                {/each}
              </div>
              <p>
                waiting for a useful guest · {coldWait.ms.toLocaleString()} ms
              </p>
              <p>
                warm: restore from the snapshot (every request) <span
                  class="num">{Math.round(firstRestore.total)} ms</span
                >
              </p>
              <div class="timing-bar" aria-hidden="true">
                <span
                  class="restore-segment"
                  style={`width: ${restoreBarFraction(firstRestore, cold) * 100}%`}
                ></span>
              </div>
              <p>same scale. really.</p>
            </div>
            <dl class="spec phase-spec" aria-label="Cold phases">
              {#each cold.phases as phase (phase.name)}
                <div class="row">
                  <dt>
                    <span
                      class="phase-key"
                      aria-hidden="true"
                      style={`background: var(--fc-phase-${phase.name.replace(/_/g, "-")})`}
                    ></span>{phase.name}
                  </dt>
                  <dd class="num">{phase.ms.toFixed(1)} ms</dd>
                </div>
              {/each}
            </dl>
          </div>
        </section>

        <section class="edition" id="freeze" aria-labelledby="freeze-title">
          <div class="ed-head">
            <span class="num">02</span><span>freezing · frozen</span>
          </div>
          <div class="ed-lead">
            <h2 id="freeze-title">
              So the daemon builds it once, then freezes it.
            </h2>
            <p>
              Every page of guest RAM and every device register is written to
              disk: a memory file and a vmstate file. Saving the snapshot takes
              <span class="num">{coldSave.ms.toLocaleString()}&nbsp;ms</span>,
              paid one time at startup.
            </p>
          </div>
          <div class="ed-body prose">
            <Diagram
              label="Every page of guest RAM and every device register is written to disk"
            >
              <DBox sub="guest RAM">A · microVM</DBox>
              <DArrow />
              <DGroup label="B · frozen">
                <DBox role="store" sub="every page of guest RAM">base.mem</DBox>
                <DBox role="store" sub="device + vcpu state">base.vmstate</DBox>
              </DGroup>
            </Diagram>
          </div>
        </section>

        <section class="edition" id="restore" aria-labelledby="restore-title">
          <div class="ed-head">
            <span class="num">03</span><span>restoring · resumed</span>
          </div>
          <div class="ed-lead">
            <h2 id="restore-title">A request restores.</h2>
            <p>
              The bytes map from disk straight back into memory and the guest
              resumes mid-thought. The readiness wait that cost
              <span class="num">{coldWait.ms.toLocaleString()}&nbsp;ms</span>
              cold takes
              <span class="num"
                >{firstRestore.phases
                  .find((p) => p.name === "guest_wait_ready")
                  .ms.toFixed(1)}&nbsp;ms</span
              >
              warm. The whole wake-up:
              <span class="num">~{Math.round(sandboxRestoreMs)}&nbsp;ms</span>.
            </p>
          </div>
          <div class="ed-body prose">
            <Diagram label="disk → memory">
              <DGroup label="B · frozen">
                <DBox role="store">base.mem</DBox><DBox role="store"
                  >base.vmstate</DBox
                >
              </DGroup>
              <DArrow />
              <DBox sub="guest RAM · resumed">A · microVM</DBox>
            </Diagram>
            <p>that sliver, magnified · run #1 of {restores.length}</p>
            <div class="timing-bar" aria-hidden="true">
              {#each firstRestore.phases.filter((p) => p.ms > 0) as phase (phase.name)}
                <span
                  style={`width: ${(phase.ms / firstRestore.total) * 100}%; background: var(--fc-phase-${phase.name.replace(/_/g, "-")})`}
                ></span>
              {/each}
            </div>
            <dl class="spec" aria-label="Warm phases">
              {#each firstRestore.phases as phase (phase.name)}
                <div class="row">
                  <dt>
                    <span
                      class="phase-key"
                      aria-hidden="true"
                      style={`background: var(--fc-phase-${phase.name.replace(/_/g, "-")})`}
                    ></span>{PHASE_HUMAN[phase.name]}
                  </dt>
                  <dd class="num">{phase.ms.toFixed(1)} ms</dd>
                </div>
              {/each}
            </dl>
          </div>
        </section>

        <section class="edition" id="runs" aria-labelledby="runs-title">
          <div class="ed-head">
            <span class="num">04</span><span
              >Warm, every request: <span class="num"
                >{restoreTotalMin.toFixed(0)}&ndash;{restoreTotalMax.toFixed(0)} ms</span
              ></span
            >
          </div>
          <div class="ed-lead">
            <h2 id="runs-title">And again. And again.</h2>
            <p>
              Each restore is a fresh, isolated VM from the same frozen image,
              then discarded. {restores.length} real recorded runs, end to end, including
              auth and code execution.
            </p>
            <p>
              The snapshot restore itself is a steady ~{Math.round(
                sandboxRestoreMs,
              )} ms across all {restores.length} recorded runs.
            </p>
          </div>
          <ul class="divided recorded-runs" aria-label="Recorded runs">
            {#each restores as run, i (i)}
              <li>
                run {String(i + 1).padStart(2, "0")}
                <span class="num">{run.total.toFixed(1)} ms</span>
              </li>
            {/each}
          </ul>
        </section>

        <section class="edition" id="replay" aria-labelledby="replay-title">
          <div class="ed-head">
            <span class="num">05</span><span>Replay</span>
          </div>
          <div class="ed-lead">
            <h2 id="replay-title">Feel it. Restore one now.</h2>
            <p>
              This button replays one of the {restores.length} recorded runs at its
              true speed. No cluster is touched: the timings below were captured from
              the live daemon and baked into this page.
            </p>
          </div>
          <ReplayWidget {restores} {PHASE_HUMAN} />
        </section>
      </div>
    </div>
  </div>
</main>
