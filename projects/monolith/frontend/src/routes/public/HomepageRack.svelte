<script>
  // The timing figure comes from metrics.js so it is not a hardcoded
  // literal: the FIRECRACKER card quotes the trace-derived sandbox restore
  // (sandboxRestoreMs, the demo it links to).
  import { sandboxRestoreMs } from "$lib/public/fcstory/metrics.js";
  import { apps } from "$lib/public/apps.js";

  // Live-maps chips render from the shared registry instead of a hardcoded
  // anchor list, keyed by slug so this stays a fixed, curated subset (not
  // "every non-featured app") if the registry grows.
  const liveMapSlugs = ["ships", "stars", "hikes", "trips"];
  const liveMaps = liveMapSlugs.map((slug) =>
    apps.find((a) => a.slug === slug),
  );
  const featuredApps = apps.filter((a) => a.featured);
</script>

<section
  class="rack-section"
  id="homelab"
  aria-label="Homelab hardware and systems"
>
  <p class="rack-eyebrow">
    4 nodes &middot; 52 CPUs &middot; 112 GB &middot; one RTX 4090
  </p>
  <h2>HOMELAB</h2>

  <div class="rack-grid">
    <div class="rack">
      <div class="node">
        <span class="nname">NODE-1</span>
        <div class="nrole">12 cpu &middot; 16 gb &middot; postgres primary</div>
      </div>
      <div class="node">
        <span class="nname">NODE-2</span>
        <div class="nrole">12 cpu &middot; 16 gb &middot; serves this page</div>
      </div>
      <div class="node">
        <span class="nname">NODE-3</span>
        <div class="nrole">12 cpu &middot; 16 gb &middot; storage replicas</div>
      </div>
      <div class="node gpu">
        <span class="nname">NODE-4</span>
        <div class="nrole">
          16 cpu &middot; 64 gb &middot; rtx 4090 &middot; microvms
        </div>
      </div>
      <div class="maps">
        <span class="lbl">Live maps</span>
        <div class="chips">
          {#each liveMaps as app (app.slug)}
            <a href={app.href}>{app.label}</a>
          {/each}
        </div>
      </div>
    </div>

    <div class="callouts">
      {#each featuredApps as app (app.slug)}
        <div class="callout featured">
          <div class="chead">
            <h3>{app.label.toUpperCase()}</h3>
            <span class="where"
              >{app.slug === "grimoire" ? "NODE-2" : "NODE-4"}</span
            >
          </div>
          {#if app.slug === "grimoire"}
            <p>
              A D&amp;D campaign manager built on grants: the same monster
              renders full stats for the DM, redacted stats for a player who has
              fought it, and just a name for one who has only heard rumours.
              <a class="more" href="/app/grimoire">play &rarr;</a>
            </p>
          {:else if app.slug === "firecracker"}
            <p>
              Boot a workload once, freeze it, restore the snapshot for every
              request. The guest never holds a real secret; an egress proxy
              swaps placeholder tokens for credentials at the network hop. Watch
              a microVM restore from disk in <b>{sandboxRestoreMs}ms</b>.
              <a class="more" href="/ember/firecracker"
                >watch it restore &rarr;</a
              >
            </p>
          {/if}
        </div>
      {/each}
      <div class="hood">
        <span class="lbl">Under the hood</span>
        <ul>
          <li>
            Tag <b>@Bosun</b> in a Discord thread and an agent answers;
            <b>EmberVM</b>
            gives every job its own microVM.
            <a class="more" href="/ember">how &rarr;</a>
          </li>
          <li>
            A <b>35B model</b> on the RTX 4090 answers at
            <b>~170 tokens a second</b>; chat, the agents, and note search share
            it.
          </li>
          <li>
            One Postgres backs every app; <a class="more" href="/app/notes"
              >notes</a
            > is a public RAG over it.
          </li>
          <li>
            Bazel builds every image, ArgoCD ships the repo. <a
              class="more"
              href="/docs">the pipeline &rarr;</a
            >
          </li>
        </ul>
      </div>
    </div>
  </div>
</section>

<style>
  .rack-section {
    max-width: 1360px;
    margin: 0 auto;
    padding: 48px 32px;
  }
  .rack-eyebrow {
    display: inline-block;
    font-family: var(--mono);
    font-size: 14px;
    font-weight: 700;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--accent);
    background: var(--ink);
    padding: 4px 10px;
    margin: 0;
  }
  h2 {
    font-family: var(--mono);
    font-size: 28px;
    font-weight: 800;
    letter-spacing: 0.02em;
    margin: 8px 0 22px;
  }
  .rack-grid {
    display: grid;
    grid-template-columns: minmax(300px, 5fr) minmax(320px, 6fr);
    gap: 32px;
    align-items: start;
  }
  .rack {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .node {
    border: 2px solid var(--ink);
    background: var(--paper);
    padding: 16px 18px;
  }
  .nname {
    font-family: var(--mono);
    font-size: 15px;
    font-weight: 800;
    letter-spacing: 0.06em;
  }
  .nrole {
    font-family: var(--mono);
    font-size: 12px;
    color: var(--ink-2);
    margin-top: 4px;
    letter-spacing: 0.03em;
  }
  .node.gpu {
    background: var(--ink);
  }
  .node.gpu .nname {
    color: var(--accent);
  }
  .node.gpu .nrole {
    color: var(--paper);
  }
  .maps {
    margin-top: 14px;
    border-top: 2px dashed var(--rule);
    padding-top: 16px;
  }
  .maps .lbl {
    display: block;
    font-family: var(--mono);
    font-size: 13px;
    font-weight: 800;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    margin-bottom: 10px;
  }
  .maps .chips {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px;
  }
  .maps .chips a {
    font-family: var(--mono);
    font-size: 13px;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--ink);
    text-decoration: none;
    border: 2px solid var(--ink);
    padding: 12px 16px;
    background: var(--paper);
    text-align: center;
  }
  .maps .chips a:hover {
    background: var(--accent);
  }
  .callouts {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .callout {
    background: var(--paper);
    border: 2px solid var(--ink);
    padding: 16px 18px 15px;
  }
  /* Flagship cards (Grimoire, Firecracker) get the same border/padding as
     every other callout, plus a solid accent top rule so they read as the
     headline pieces without inventing a new card style. */
  .callout.featured {
    border-top: 6px solid var(--accent);
  }
  .callout.featured .where {
    background: var(--accent);
  }
  .chead {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 12px;
    margin-bottom: 6px;
  }
  .chead h3 {
    font-family: var(--mono);
    font-size: 13px;
    letter-spacing: 0.05em;
    margin: 0;
  }
  .where {
    font-family: var(--mono);
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.1em;
    border: 1px solid var(--ink);
    padding: 2px 7px;
    white-space: nowrap;
  }
  .callout p {
    font-size: 13.5px;
    line-height: 1.55;
    color: var(--ink);
    margin: 0;
  }
  .more {
    font-family: var(--mono);
    font-size: 10.5px;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--ink);
    text-decoration: none;
    border-bottom: 2px solid var(--accent);
  }
  .more:hover {
    background: var(--accent);
  }
  .hood {
    padding: 2px 2px 0;
  }
  .hood .lbl {
    display: block;
    font-family: var(--mono);
    font-size: 13px;
    font-weight: 800;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--ink-2);
    margin-bottom: 10px;
  }
  .hood ul {
    display: flex;
    flex-direction: column;
    gap: 8px;
    margin: 0;
    padding-left: 18px;
  }
  .hood li {
    font-size: 13px;
    line-height: 1.4;
    color: var(--ink-2);
  }
  .hood b {
    color: var(--ink);
  }
  @media (max-width: 820px) {
    .rack-grid {
      grid-template-columns: 1fr;
    }
  }
</style>
