<script>
  // The Agent Platform callout describes the same restore path as the
  // /app/firecracker explainer, so it quotes the same trace-derived
  // sandbox restore figure rather than a hardcoded literal.
  import {
    sandboxRestoreMs,
    agentFirstModelCallMs,
  } from "$lib/public/fcstory/metrics.js";
</script>

<section class="rack-section" id="homelab" aria-label="Homelab hardware and systems">
  <p class="rack-eyebrow">4 nodes &middot; 52 CPUs &middot; 112 GB &middot; one RTX 4090</p>
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
        <div class="nrole">16 cpu &middot; 64 gb &middot; rtx 4090 &middot; firecracker</div>
      </div>
      <div class="maps">
        <span class="lbl">Live maps</span>
        <div class="chips">
          <a href="/app/ships">Ships</a>
          <a href="/app/stars">Stars</a>
          <a href="/app/hikes">Hikes</a>
          <a href="/app/trips">Trips</a>
        </div>
      </div>
    </div>

    <div class="callouts">
      <div class="callout">
        <div class="chead">
          <h3>AGENT PLATFORM</h3>
          <span class="where">NODE-4</span>
        </div>
        <p>
          A stateless daemon serves <code>POST /invoke/&#123;workload&#125;</code>: restore a
          copy-on-write snapshot in <b>{sandboxRestoreMs}ms</b> (~{agentFirstModelCallMs}ms cold start
          to first model call), reverse-proxy over vsock into the microVM. The guest never holds
          a real secret; an egress proxy swaps placeholder tokens for credentials at the network
          hop. Coding agents and Semgrep scans run as peers on the same substrate.
          <a class="more" href="/docs/agents">how &rarr;</a>
        </p>
      </div>
      <div class="callout">
        <div class="chead">
          <h3>INFERENCE</h3>
          <span class="where">NODE-4</span>
        </div>
        <p>
          vLLM serving a <b>35B sparse-MoE</b> (~3B active), int4-mixed weights with an fp8
          KV-cache, <b>~170 tok/s</b> single-stream decode. Chat, the agents, and the knowledge
          graph's RAG all share the 4090.
        </p>
      </div>
      <div class="callout">
        <div class="chead">
          <h3>POSTGRES</h3>
          <span class="where">NODE-1</span>
        </div>
        <p>
          Postgres + pgvector holds the apps, a fileless knowledge graph, and the embeddings;
          <a class="more" href="/app/notes">notes</a> is a public RAG over it. Declarative
          migrations applied by an operator, volumes replicated across nodes.
        </p>
      </div>
      <div class="callout">
        <div class="chead">
          <h3>PLATFORM PLUMBING</h3>
          <span class="where">ALL NODES</span>
        </div>
        <p>
          Five custom Bazel rulesets build every image dual-arch and pin digests into versioned
          OCI Helm charts; ArgoCD reconciles the cluster from the repo. <b>280+ chart
          versions</b> so far.
          <a class="more" href="/docs/contributing">the pipeline &rarr;</a>
        </p>
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
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--ink);
    background: var(--blue);
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
  .callout code {
    font-family: var(--mono);
    font-size: 12px;
    background: var(--cream);
    padding: 1px 4px;
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
  @media (max-width: 820px) {
    .rack-grid {
      grid-template-columns: 1fr;
    }
  }
</style>
