# Engineering Page Migration Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or
> superpowers:subagent-driven-development in-session) to implement this plan
> task-by-task.

**Goal:** Replace the legacy Astro engineering page with a neo-brutalist
`/engineering` route in the monolith SvelteKit frontend, with refreshed
content for 11 systems (8 migrated, 3 new) and hand-built CSS/SVG diagrams.

**Architecture:** A static SvelteKit route at
`src/routes/public/engineering/` driven by a content data module
(`engineering-data.js`, mirroring the CV page's `cv-data.js` pattern). An
expo card grid anchors down to per-project deep-dive sections. Diagrams are
per-project Svelte components composed from four shared primitives, no
Mermaid, no CDN. Design doc:
`docs/plans/2026-06-10-engineering-page-migration-design.md`.

**Tech Stack:** Svelte 5 (runes: `$props`, `$derived`), SvelteKit
file-based routing, the existing design system
(`$lib/public/styles/design-system.css`), vitest for the data-shape test.

---

## Repo facts the implementer must know

- **No local test runs.** Do not run `vitest`, `pnpm test`, or
  `bazel test` from the workstation (CLAUDE.md policy). Write the tests,
  commit, and defer execution to BuildBuddy CI on the pushed branch.
  `format` (standalone formatter) IS allowed and must be run before the
  final commit.
- **No BUILD edits needed.** `projects/monolith/frontend/BUILD` is
  `# gazelle:ignore` and globs `src/**/*` (tests are globbed by the vitest
  target via `src/**/*.test.js`). New `.svelte`/`.js` files under `src/`
  are picked up automatically.
- **Routing is automatic.** `src/hooks.js` reroutes
  `public.jomcgi.dev/engineering` to `/public/engineering` internally. The
  only nav-related code change is in `src/routes/+layout.svelte`
  (active-state derivation) and `Nav.svelte` (the href).
- **Worktree:** all work happens in `/tmp/claude-worktrees/engineering-page`
  on branch `feat/engineering-page-migration`.
- **Never use em-dashes** in any copy, comment, or commit message. Use
  commas, colons, or parentheses.
- **Svelte 5 idiom:** components in this codebase use runes (`$props()`,
  `$derived`), snippets (`{#snippet}` / `{@render}`), and scoped
  `<style>` blocks. Match `cv/+page.svelte` as the style reference.
- The old page being migrated (content source of truth for the 8 carried
  sections): `projects/websites/jomcgi.dev/src/pages/engineering.astro`.
  It is NOT modified by this plan.

---

### Task 1: Content data module + shape test

**Files:**

- Create: `projects/monolith/frontend/src/routes/public/engineering/engineering-data.js`
- Create: `projects/monolith/frontend/src/routes/public/engineering/engineering-data.test.js`

**Step 1: Write the test**

`engineering-data.test.js`:

```js
import { describe, it, expect } from "vitest";
import {
  intro,
  marqueeItems,
  categories,
  projects,
} from "./engineering-data.js";

describe("engineering-data", () => {
  it("has hero content", () => {
    expect(intro.title).toBeTruthy();
    expect(intro.lede).toBeTruthy();
    expect(marqueeItems.length).toBeGreaterThan(5);
  });

  it("every project has the required fields", () => {
    expect(projects.length).toBe(11);
    for (const p of projects) {
      expect(p.id, p.title).toMatch(/^[a-z0-9-]+$/);
      expect(p.title).toBeTruthy();
      expect(
        categories[p.category],
        `unknown category on ${p.id}`,
      ).toBeTruthy();
      expect(p.oneLiner).toBeTruthy();
      expect(p.motivation).toBeTruthy();
      expect(p.facts.length).toBeGreaterThanOrEqual(3);
      for (const f of p.facts) {
        expect(f.k).toBeTruthy();
        expect(f.v).toBeTruthy();
      }
      for (const l of p.links ?? []) {
        expect(l.label).toBeTruthy();
        expect(l.href).toMatch(/^(https:\/\/|\/)/);
      }
    }
  });

  it("project ids are unique (they become DOM anchors)", () => {
    const ids = projects.map((p) => p.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("copy contains no em-dashes", () => {
    const blob = JSON.stringify({ intro, marqueeItems, projects });
    expect(blob).not.toContain("—");
  });
});
```

**Step 2: Write the data module**

`engineering-data.js`, complete content below. This copy is final (already
fact-checked against the repo, CV, and current cluster state); implementers
must not rewrite it, only transcribe:

```js
// Engineering page content, the single source of truth for the expo grid
// and the deep-dive sections. Facts were checked against the repo and CV
// at migration time (2026-06). When a system changes, edit it here.

export const intro = {
  eyebrow: "Field Notes",
  title: "Engineering",
  lede: "Deep dives into systems running on a bare-metal K3s cluster I operate at home, deployed with GitOps. Each one covers the motivation and the execution.",
  stickers: ["11 Systems", "Bare Metal", "GitOps"],
  source: {
    label: "github/jomcgi/homelab",
    href: "https://github.com/jomcgi/homelab",
  },
};

export const marqueeItems = [
  "Go",
  "Rust",
  "Python",
  "Kubernetes Operators",
  "NATS JetStream",
  "vLLM on a 4090",
  "Postgres + pgvector",
  "Bazel + BuildBuddy",
  "ArgoCD GitOps",
  "Linkerd",
  "Cloudflare Tunnel",
  "SigNoz",
];

export const categories = {
  agents: { label: "Agents", color: "var(--coral)" },
  data: { label: "Data", color: "var(--blue)" },
  operators: { label: "Operators", color: "var(--teal)" },
  apps: { label: "Apps", color: "var(--green)" },
  build: { label: "Build", color: "var(--accent)" },
};

export const projects = [
  {
    id: "agent-platform",
    category: "agents",
    title: "Agent Platform",
    oneLiner:
      "Autonomous Claude and Goose agents in sandboxed Kubernetes pods, dispatched over NATS, every tool call governed by an MCP gateway.",
    motivation:
      "I wanted agents that do real platform work: triage alerts, fix failing PRs, keep docs fresh. That means handing an LLM tools with blast radius, so the platform is built around containment. Every agent runs in its own sandbox pod, and every tool call passes through a gateway that knows who is asking.",
    facts: [
      {
        k: "Orchestrator",
        v: "A Go service dispatches agent jobs over NATS JetStream. Each job becomes an isolated sandbox pod with its own workspace, credentials, and lifecycle.",
      },
      {
        k: "Context Forge",
        v: "A self-built MCP gateway in front of every tool. Agents get RBAC-scoped toolsets per team; tool registrations are validated before they are exposed.",
      },
      {
        k: "Cluster agents",
        v: "Scheduled autonomous loops (patrol, test coverage, README freshness, PR fixer) that investigate the cluster and open PRs against this repo.",
      },
      {
        k: "Local inference",
        v: "vLLM serves a Qwen3.6 MoE (35B-A3B, int4) on a single RTX 4090 for routine tasks. Frontier models are called over API only where the task warrants it.",
      },
      {
        k: "Scheduled routines",
        v: "Claude routines run recurring maintenance on a cron, leasing jobs from the cluster over MCP and reporting results back to the knowledge graph.",
      },
    ],
    links: [
      {
        label: "projects/agent_platform",
        href: "https://github.com/jomcgi/homelab/tree/main/projects/agent_platform",
      },
    ],
  },
  {
    id: "knowledge-graph",
    category: "agents",
    title: "Knowledge Graph",
    oneLiner:
      "An LLM pipeline that decomposes my notes into structured facts, embeds them, and serves semantic search to agents and to this site.",
    motivation:
      "Notes are only useful if they come back at the right moment. The knowledge pipeline turns markdown into a queryable graph: an on-cluster LLM decomposes each note into atomic facts, critiques its own extraction, and stores embeddings for semantic recall.",
    facts: [
      {
        k: "Decomposition",
        v: "An on-cluster Qwen model decomposes markdown into structured facts, with a self-critique pass before anything is committed to the graph.",
      },
      {
        k: "Embeddings",
        v: "voyage embeddings stored in Postgres pgvector with HNSW indexes. One database holds facts, edges, and vectors; no separate vector store to run.",
      },
      {
        k: "MCP surface",
        v: "Search, notes, tasks, and research-gap tools exposed over MCP, so any Claude session (local or scheduled) can read and write the graph.",
      },
      {
        k: "Gap research",
        v: "The graph files research gaps for itself. External questions are auto-researched by agents; judgment calls queue for human review.",
      },
      {
        k: "This site",
        v: "The notes view and Cmd+K search overlay on this site render the same live graph through the same API.",
      },
    ],
    links: [
      { label: "Browse the notes", href: "/notes" },
      {
        label: "projects/monolith/knowledge",
        href: "https://github.com/jomcgi/homelab/tree/main/projects/monolith/knowledge",
      },
    ],
  },
  {
    id: "loom",
    category: "data",
    status: "Pre-alpha",
    title: "Loom",
    oneLiner:
      "An open-source take on Palantir Foundry: a typed-object data platform with lineage and governance built in, on a Rust + DataFusion + DuckLake core.",
    motivation:
      "Operating a governed data platform should scale sub-linearly with data and org size: a new dataset, domain, or transform should add no new system to run. Loom is a collaborative bet on that premise, with governance treated as a safety property (STPA hazard analysis, where unsafe means a governance violation, not a crash).",
    facts: [
      {
        k: "Status",
        v: "Pre-alpha, built with a collaborator, to be open-sourced. The control-plane library exists; services are in progress.",
      },
      {
        k: "Control plane",
        v: "A single Postgres owns queue, catalog, ontology, ACL, and lineage as schema-separated concerns. The job queue is SKIP LOCKED plus LISTEN/NOTIFY, no broker.",
      },
      {
        k: "Compute",
        v: "Rust services embed Apache DataFusion. Tables are DuckLake on S3-compatible object storage, with the catalog in the same Postgres.",
      },
      {
        k: "Wire protocol",
        v: "Quack: DuckDB-compatible remote access, so existing DuckDB clients connect without a custom driver.",
      },
      {
        k: "Governance",
        v: "OpenLineage events plus STPA-derived constraints. The ontology and ACLs live next to the data they govern, not in a bolt-on service.",
      },
    ],
    links: [],
  },
  {
    id: "oci-model-cache",
    category: "operators",
    title: "OCI Model Cache Operator",
    oneLiner:
      "Reference a HuggingFace model in a pod spec like a container image; the operator caches it in an OCI registry and rewrites the pod at admission.",
    motivation:
      "HuggingFace models are huge and slow to download. I wanted to reference a model in a pod spec the same way you reference a container image, and have it just work. The operator caches models in an OCI registry and streams them to pods without touching disk.",
    facts: [
      {
        k: "PodMutator",
        v: "An admission webhook intercepts pods with hf.co/ volume references, resolves the OCI ref synchronously (pod specs are immutable after admission), creates a ModelCache CR, and gates scheduling until the model is synced.",
      },
      {
        k: "State machine",
        v: "Built with Sextant: Pending, Resolving, Syncing, Ready, with a Failed state. Guards distinguish permanent errors from transient failures for automatic retry.",
      },
      {
        k: "hf2oci",
        v: "Streams HuggingFace models into OCI layers: HTTP response to tar to io.Pipe to registry push. Zero disk I/O. Safetensors and GGUF formats.",
      },
      {
        k: "Smart naming",
        v: "The HuggingFace baseModels API resolves derivative models to their base, so derivatives share OCI layers with the base repo for deduplication.",
      },
    ],
    snippet: {
      lang: "yaml",
      code: "volumes:\n  - name: model\n    image:\n      reference: hf.co/NousResearch/Hermes-3-Llama-3.1-8B-GGUF",
    },
    links: [
      {
        label: "projects/operators/oci-model-cache",
        href: "https://github.com/jomcgi/homelab/tree/main/projects/operators/oci-model-cache",
      },
    ],
  },
  {
    id: "sextant",
    category: "operators",
    title: "Sextant",
    oneLiner:
      "A code generator that turns YAML state-machine specs into type-safe Go, so invalid operator state transitions become compile errors.",
    motivation:
      "Kubernetes operators are state machines, but we write them as imperative reconciliation loops. Every operator I wrote had the same bugs: invalid state transitions, forgotten error handling, missing metrics. Sextant defines the state machine declaratively and generates the boilerplate.",
    facts: [
      {
        k: "Compile-time safety",
        v: "Each state is a Go struct and transitions return the next state's type. Going from Pending to Ready without passing through Creating is a compiler error.",
      },
      {
        k: "Forced idempotency",
        v: "Transition methods require request IDs in their signatures, which forces you to call the external API and record its ID before transitioning.",
      },
      {
        k: "Guard conditions",
        v: "Go expressions embedded in the YAML, evaluated at transition time. Invalid expressions fail at code-generation time, not in production.",
      },
      {
        k: "CI drift guard",
        v: "Generated code is committed, and CI regenerates from the spec and fails on any drift. The spec next to the operator is always the truth.",
      },
      {
        k: "Generated metrics",
        v: "Prometheus counters, histograms, and a state-duration gauge per machine, with automatic cleanup on resource deletion.",
      },
    ],
    snippet: {
      lang: "yaml",
      code: "states:\n  - name: Pending\n    initial: true\n  - name: Creating\n    fields:\n      requestID: string\n  - name: Ready\n    terminal: true\n\ntransitions:\n  - from: Pending\n    to: Creating\n    action: StartCreation\n    params:\n      - requestID: string",
    },
    links: [
      {
        label: "projects/sextant",
        href: "https://github.com/jomcgi/homelab/tree/main/projects/sextant",
      },
    ],
  },
  {
    id: "cloudflare-operator",
    category: "operators",
    title: "Cloudflare Operator",
    oneLiner:
      "Annotate a Deployment and get DNS, a Zero Trust app, and tunnel routing provisioned automatically. Zero Trust ingress without the toil.",
    motivation:
      "Every new service meant clicking through the Cloudflare dashboard: create a DNS record, create a Zero Trust application, update the tunnel config. I wanted to annotate a Deployment and have everything provisioned automatically.",
    facts: [
      {
        k: "Annotation-driven",
        v: "cloudflare.ingress.hostname and cloudflare.zero-trust.policy annotations trigger reconciliation. No CRDs to manage for the common case.",
      },
      {
        k: "State machine",
        v: "Built with Sextant. Pending to CreatingDNS to CreatingZTApp to UpdatingConfig to Ready, each step idempotent.",
      },
      {
        k: "Finalizers",
        v: "Deleting the Deployment cleans up DNS records, Zero Trust apps, and tunnel routes. No orphaned Cloudflare resources.",
      },
      {
        k: "Drift detection",
        v: "Periodic reconciliation reverts manual dashboard edits. The operator is the source of truth.",
      },
    ],
    snippet: {
      lang: "yaml",
      code: "metadata:\n  annotations:\n    cloudflare.ingress.hostname: myapp.jomcgi.dev\n    cloudflare.zero-trust.policy: joe-only",
    },
    links: [],
  },
  {
    id: "trips",
    category: "apps",
    title: "Trips: Camera to Browser",
    oneLiner:
      "A dash-mounted GoPro becomes a live trip feed: EXIF extraction, event sourcing on NATS, on-the-fly image resizing, edge caching.",
    motivation:
      "I wanted an easy way to share trips with friends and family. A GoPro on the dash captures photos automatically, and my homelab turns them into a live feed they can follow along with, or replay later. Works for anything with GPS-tagged photos.",
    facts: [
      {
        k: "Capture",
        v: "A Python asyncio controller drives the camera over WiFi: GPS-triggered interval capture, a persistent SQLite download queue, and exponential backoff on connection drops.",
      },
      {
        k: "Event store",
        v: "Trip points are events in NATS JetStream. The API replays the stream on startup to rebuild state (about 200ms for 10k events) and deletions are tombstone events. No database.",
      },
      {
        k: "Delivery",
        v: "27MB originals live in SeaweedFS; imgproxy resizes on the fly and Cloudflare CDN caches at the edge. Content-addressed keys mean cache invalidation is never needed.",
      },
      {
        k: "Display",
        v: "MapLibre vector tiles with terrain hillshade, day-by-day galleries, elevation profiles, and WebSocket live updates during active trips.",
      },
    ],
    links: [
      { label: "Live at trips.jomcgi.dev", href: "https://trips.jomcgi.dev" },
    ],
  },
  {
    id: "ships",
    category: "apps",
    title: "Ships: AIS Vessel Tracking",
    oneLiner:
      "Live maritime traffic on a map: AIS position reports streamed through the cluster to a MapLibre frontend over WebSockets.",
    motivation:
      "Living near the coast, I wanted to see what ships are passing by in real time. AIS data is publicly broadcast by vessels, but there is no simple way to visualize it locally. This pipeline streams AIS data through my cluster to a live map.",
    facts: [
      {
        k: "AIS ingest",
        v: "A Python service holds a WebSocket to AISStream.io, filters to a Pacific Northwest bounding box, and publishes position reports to NATS JetStream.",
      },
      {
        k: "Event sourcing",
        v: "Same pattern as Trips: JetStream is the source of truth and the API replays the stream on startup to rebuild vessel state.",
      },
      {
        k: "Ships API",
        v: "REST plus WebSocket streaming, with position deduplication to cut noise from stationary vessels.",
      },
      {
        k: "MapLibre UI",
        v: "Vessels render as directional arrows by heading. Click for ship type, speed, course, and destination.",
      },
    ],
    links: [
      { label: "Live at ships.jomcgi.dev", href: "https://ships.jomcgi.dev" },
    ],
  },
  {
    id: "stargazer",
    category: "apps",
    title: "Stargazer",
    oneLiner:
      "Scores stargazing locations by combining light pollution, road access, elevation, and rolling weather forecasts into one number.",
    motivation:
      "Finding good stargazing spots means combining light pollution maps, road access, elevation for horizon clearance, and the weather forecast. Stargazer scores locations across Scotland on all of these and updates continuously.",
    facts: [
      {
        k: "16-task DAG",
        v: "Parallel acquisition of the light pollution atlas, OSM road network, SRTM elevation, and MET Norway forecasts, scheduled by dependency.",
      },
      {
        k: "Spatial analysis",
        v: "Dark-region extraction from the light pollution raster, road buffering for accessibility, zone classification by sky quality.",
      },
      {
        k: "Weather scoring",
        v: "Cloud cover, humidity, fog probability, wind, and dew point with configurable weights, refreshed hourly.",
      },
      {
        k: "Composite score",
        v: "0 to 100: darkness 40%, weather 25%, accessibility 20%, horizon 15%. Filterable by threshold.",
      },
    ],
    links: [],
  },
  {
    id: "bazel",
    category: "build",
    title: "Bazel: One Way to Build Everything",
    oneLiner:
      "One build system for Go, Python, JS, Helm charts, and container images, with custom Starlark rulesets and remote execution on BuildBuddy.",
    motivation:
      "I got tired of different build commands for every project. I wanted one system that works the same everywhere: laptop, CI, and agents working in the cluster. Everything is vendored, so there is nothing to install beyond Bazel itself.",
    facts: [
      {
        k: "format",
        v: "One command runs every formatter, regenerates manifests and lock files in parallel, and finishes in seconds when nothing changed.",
      },
      {
        k: "Custom rulesets",
        v: "rules_helm (lint, template, package, OCI-push charts, plus an ArgoCD application macro), rules_semgrep, rules_wrangler for Cloudflare Pages, and apko image tooling. Each ships a Gazelle extension that writes the BUILD files.",
      },
      {
        k: "GitOps manifests",
        v: "Helm charts render through Bazel into the source tree, so PR diffs show exactly what changes in the cluster before it deploys.",
      },
      {
        k: "Multi-platform images",
        v: "apko Alpine images from YAML with pinned lock files. One target builds arm64 and amd64 and pushes a multi-platform index.",
      },
      {
        k: "BuildBuddy RBE",
        v: "All builds run remotely with a shared content-addressed cache. Unchanged code never rebuilds.",
      },
    ],
    links: [
      {
        label: "bazel/",
        href: "https://github.com/jomcgi/homelab/tree/main/bazel",
      },
    ],
  },
  {
    id: "rules-semgrep",
    category: "build",
    title: "rules_semgrep",
    oneLiner:
      "Hermetic Semgrep static and supply-chain analysis as Bazel tests: digest-pinned OCaml engine, cached diff scans in 30 seconds.",
    motivation:
      "Semgrep on managed CI took 2+ minutes per diff scan and rule-registry fetches made results non-deterministic. I needed scans that run in seconds, produce identical results from identical inputs, and only re-run when something changed. Bazel's content-addressed cache gives all three, but Semgrep had no Bazel integration.",
    facts: [
      {
        k: "No Python",
        v: "Extracts the semgrep-core OCaml binary from PyPI wheels and vendors it as an OCI artifact on GHCR, bypassing the Python wrapper and its startup tax.",
      },
      {
        k: "Digest-pinned",
        v: "Engine binaries and Pro rule packs are pinned to sha256 digests; a daily job updates digests and opens a PR. Same inputs, same results.",
      },
      {
        k: "Three rule types",
        v: "semgrep_test for sources, semgrep_manifest_test for Helm-rendered YAML, semgrep_target_test for transitive deps via aspect. Gazelle generates all of them.",
      },
      {
        k: "Supply chain",
        v: "SCA lockfile scanning with Pro reachability, auto-detected from @pip and @npm dependency prefixes. Zero config.",
      },
      {
        k: "Results",
        v: "Cached diff scans in 30 seconds, down from 2+ minutes. Cold cache: 4 minutes for all tests, images, and scans.",
      },
    ],
    links: [
      {
        label: "bazel/semgrep",
        href: "https://github.com/jomcgi/homelab/tree/main/bazel/semgrep",
      },
    ],
  },
];
```

**Step 3: Self-review the transcription**

Diff your file against this plan section. Verify: 11 entries, ids all
kebab-case, no em-dash characters anywhere (`grep -n '—' engineering-data.js`
must return nothing).

**Step 4: Commit**

```bash
git add projects/monolith/frontend/src/routes/public/engineering/
git commit -m "feat(frontend): add engineering page content module"
```

---

### Task 2: Diagram primitives

**Files:**

- Create: `projects/monolith/frontend/src/lib/public/components/diagrams/Diagram.svelte`
- Create: `projects/monolith/frontend/src/lib/public/components/diagrams/DGroup.svelte`
- Create: `projects/monolith/frontend/src/lib/public/components/diagrams/DBox.svelte`
- Create: `projects/monolith/frontend/src/lib/public/components/diagrams/DArrow.svelte`

These four primitives replace Mermaid. The model: a diagram is a horizontal
flow of nodes and labeled groups separated by arrows; on narrow screens the
flow stacks vertically and arrows rotate. Per-project diagram components
(Tasks 3 and 4) only compose these, they carry no styling of their own
beyond grid placement.

**Step 1: `Diagram.svelte`** (outer frame + flow container)

```svelte
<script>
  /**
   * Outer frame for an architecture diagram: bordered panel with an
   * eyebrow label, containing a flow of DBox/DGroup/DArrow children.
   * Flows horizontally on desktop, stacks vertically under 720px.
   * @type {{ label?: string, children: import('svelte').Snippet }}
   */
  let { label = "Architecture", children } = $props();
</script>

<figure class="diagram" role="img" aria-label={label}>
  <figcaption class="diagram-label mono">{label}</figcaption>
  <div class="diagram-flow">
    {@render children()}
  </div>
</figure>

<style>
  .diagram {
    border: 2px solid var(--ink);
    border-radius: var(--radius);
    background: var(--paper);
    padding: 20px;
    margin: 0;
  }

  .diagram-label {
    font-size: 11px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--ink-3);
    margin-bottom: 16px;
  }

  .diagram-flow {
    display: flex;
    align-items: center;
    justify-content: flex-start;
    gap: 10px;
    flex-wrap: wrap;
  }

  @media (max-width: 720px) {
    .diagram-flow {
      flex-direction: column;
      align-items: stretch;
    }
  }
</style>
```

**Step 2: `DGroup.svelte`** (the Mermaid "subgraph" replacement)

```svelte
<script>
  /**
   * Labeled cluster of nodes, the subgraph motif: dashed border, mono
   * label tab. `stack` lays children vertically (default horizontal).
   * @type {{ label: string, stack?: boolean, children: import('svelte').Snippet }}
   */
  let { label, stack = false, children } = $props();
</script>

<div class="dgroup" class:stack>
  <span class="dgroup-label mono">{label}</span>
  <div class="dgroup-body" class:stack>
    {@render children()}
  </div>
</div>

<style>
  .dgroup {
    border: 2px dashed var(--rule-2);
    border-radius: var(--radius);
    padding: 18px 12px 12px;
    position: relative;
    flex-shrink: 0;
  }

  .dgroup-label {
    position: absolute;
    top: -9px;
    left: 10px;
    background: var(--paper);
    padding: 0 6px;
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--ink-3);
  }

  .dgroup-body {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .dgroup-body.stack {
    flex-direction: column;
    align-items: stretch;
  }

  @media (max-width: 720px) {
    .dgroup {
      width: 100%;
    }
    .dgroup-body {
      flex-direction: column;
      align-items: stretch;
    }
  }
</style>
```

**Step 3: `DBox.svelte`** (node)

```svelte
<script>
  /**
   * Diagram node: hard-shadowed ink-bordered chip. `role` picks the fill
   * from a fixed palette so diagrams stay consistent across projects:
   * source (coral), process (yellow), store (blue), output (green),
   * external (paper).
   * @type {{ role?: 'source'|'process'|'store'|'output'|'external', sub?: string, children: import('svelte').Snippet }}
   */
  let { role = "process", sub = "", children } = $props();

  const fills = {
    source: "var(--coral)",
    process: "var(--accent)",
    store: "var(--blue)",
    output: "var(--green)",
    external: "var(--paper)",
  };
</script>

<span class="dbox mono" style:background={fills[role]}>
  {@render children()}
  {#if sub}<span class="dbox-sub">{sub}</span>{/if}
</span>

<style>
  .dbox {
    display: inline-flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    text-align: center;
    gap: 2px;
    border: 2px solid var(--ink);
    border-radius: 6px;
    box-shadow: var(--shadow-hard-sm);
    padding: 8px 12px;
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.04em;
    color: var(--ink);
    flex-shrink: 0;
  }

  .dbox-sub {
    font-size: 10px;
    font-weight: 400;
    color: var(--ink-2);
    letter-spacing: 0.02em;
  }
</style>
```

**Step 4: `DArrow.svelte`** (edge)

```svelte
<script>
  /**
   * Flow arrow with an optional edge label. Horizontal in a desktop
   * flow; rotates to vertical when the parent flow stacks (under 720px).
   * @type {{ label?: string }}
   */
  let { label = "" } = $props();
</script>

<span class="darrow mono" aria-hidden="true">
  {#if label}<span class="darrow-label">{label}</span>{/if}
  <svg viewBox="0 0 34 12" width="34" height="12">
    <line x1="0" y1="6" x2="26" y2="6" stroke="var(--ink)" stroke-width="2.5" />
    <path d="M24,1 L33,6 L24,11" fill="none" stroke="var(--ink)" stroke-width="2.5" stroke-linejoin="round" />
  </svg>
</span>

<style>
  .darrow {
    display: inline-flex;
    flex-direction: column;
    align-items: center;
    gap: 2px;
    flex-shrink: 0;
  }

  .darrow-label {
    font-size: 9px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--ink-3);
    white-space: nowrap;
  }

  @media (max-width: 720px) {
    .darrow svg {
      transform: rotate(90deg);
    }
    .darrow {
      align-self: center;
      flex-direction: row;
      gap: 6px;
    }
  }
</style>
```

**Step 5: Commit**

```bash
git add projects/monolith/frontend/src/lib/public/components/diagrams/
git commit -m "feat(frontend): add neo-brutalist diagram primitives"
```

---

### Task 3: Diagram components, batch 1 (agents + data + operators)

**Files:**

- Create: `projects/monolith/frontend/src/routes/public/engineering/diagrams/AgentPlatform.svelte`
- Create: `.../diagrams/KnowledgeGraph.svelte`
- Create: `.../diagrams/Loom.svelte`
- Create: `.../diagrams/OciModelCache.svelte`
- Create: `.../diagrams/Sextant.svelte`
- Create: `.../diagrams/CloudflareOperator.svelte`
- Create: `.../diagrams/index.js` (registry, extended in Task 4)

All six follow exactly the same pattern as the complete `AgentPlatform`
example below: import primitives, compose, zero local styles (grid tweaks
via the primitives' props only). Diagram components live next to the route
(not in `$lib`) because they are page content, not reusable UI.

**Step 1: `AgentPlatform.svelte`** (complete reference implementation)

```svelte
<script>
  import Diagram from "$lib/public/components/diagrams/Diagram.svelte";
  import DGroup from "$lib/public/components/diagrams/DGroup.svelte";
  import DBox from "$lib/public/components/diagrams/DBox.svelte";
  import DArrow from "$lib/public/components/diagrams/DArrow.svelte";
</script>

<Diagram label="Agent platform">
  <DGroup label="Triggers" stack>
    <DBox role="source">Routines</DBox>
    <DBox role="source">Discord</DBox>
    <DBox role="source">Alerts</DBox>
  </DGroup>
  <DArrow label="jobs" />
  <DBox role="process" sub="Go + JetStream">Orchestrator</DBox>
  <DArrow label="dispatch" />
  <DGroup label="Sandbox pod" stack>
    <DBox role="process" sub="Claude / Goose">Agent</DBox>
    <DBox role="external" sub="isolated">Workspace</DBox>
  </DGroup>
  <DArrow label="tool calls" />
  <DBox role="store" sub="MCP gateway, RBAC">Context Forge</DBox>
  <DArrow />
  <DGroup label="Tools" stack>
    <DBox role="output">Cluster</DBox>
    <DBox role="output">Knowledge graph</DBox>
    <DBox role="output" sub="vLLM on 4090">Inference</DBox>
  </DGroup>
</Diagram>
```

**Step 2: The other five.** Compose the same way from these node/edge
specs (group labels in brackets, `->` is a `DArrow`, role in parens):

- **KnowledgeGraph.svelte**, label "Knowledge pipeline":
  `[Sources, stack]: Markdown notes (source), Chat + captures (source)`
  -> "decompose" -> `LLM extraction (process, sub: Qwen, self-critique)`
  -> `[Postgres, stack]: Facts + edges (store), pgvector HNSW (store)`
  -> "serve" -> `[Consumers, stack]: MCP tools (output), Cmd+K search (output), Agents (output)`
- **Loom.svelte**, label "Loom (pre-alpha)":
  `[Services, stack]: Ingest (source), Transform (process), Query API (output)`
  -> "embed" -> `DataFusion (process, sub: Rust)`
  -> `DuckLake tables (store, sub: S3 object storage)`;
  then a second flow row: `Postgres control plane (store, sub: queue, catalog, ontology, ACL, lineage)`
  -> "speaks" -> `Quack protocol (external, sub: DuckDB-compatible)`.
  Implementation note: render two `Diagram`-level rows by wrapping each row
  in a plain `<div style="display:contents">`? No: keep it one flow and
  rely on `flex-wrap`; order boxes so the wrap break lands between the two
  conceptual rows.
- **OciModelCache.svelte**, label "Model cache":
  `Pod create (source)` -> `PodMutator (process, sub: admission webhook)`
  -> "rewrite + gate" -> `ModelCache CR (store)`
  -> `Sync job (process, sub: hf2oci)` -> `[External, stack]: HuggingFace (external), OCI registry (store)`
  -> "ready" -> `Pod ungated (output)`
- **Sextant.svelte**, label "Code generation":
  `YAML spec (source)` -> `Parse + validate (process)` -> `Generator (process)`
  -> `[Generated Go, stack]: types.go (output), transitions.go (output), metrics.go (output), status.go (output)`
  -> "drift guard" -> `CI regen check (external)`
- **CloudflareOperator.svelte**, label "Zero Trust ingress":
  `Deployment (source, sub: annotations)` -> "watch" -> `Operator (process, sub: Sextant FSM)`
  -> `[Provisioned, stack]: DNS record (output), Zero Trust app (output), Tunnel config (output)`
  -> `Cloudflare (external)`

**Step 3: Registry `index.js`**

```js
// Maps project ids from engineering-data.js to diagram components.
// Task 4 adds the apps + build entries.
import AgentPlatform from "./AgentPlatform.svelte";
import KnowledgeGraph from "./KnowledgeGraph.svelte";
import Loom from "./Loom.svelte";
import OciModelCache from "./OciModelCache.svelte";
import Sextant from "./Sextant.svelte";
import CloudflareOperator from "./CloudflareOperator.svelte";

export const diagrams = {
  "agent-platform": AgentPlatform,
  "knowledge-graph": KnowledgeGraph,
  loom: Loom,
  "oci-model-cache": OciModelCache,
  sextant: Sextant,
  "cloudflare-operator": CloudflareOperator,
};
```

**Step 4: Commit**

```bash
git add projects/monolith/frontend/src/routes/public/engineering/diagrams/
git commit -m "feat(frontend): add engineering diagrams for agents, data, operators"
```

---

### Task 4: Diagram components, batch 2 (apps + build) + registry test

**Files:**

- Create: `.../engineering/diagrams/Trips.svelte`
- Create: `.../engineering/diagrams/Ships.svelte`
- Create: `.../engineering/diagrams/Stargazer.svelte`
- Create: `.../engineering/diagrams/Bazel.svelte`
- Create: `.../engineering/diagrams/RulesSemgrep.svelte`
- Modify: `.../engineering/diagrams/index.js` (add five entries)
- Modify: `.../engineering/engineering-data.test.js` (registry coverage test)

**Step 1: Five diagrams from specs** (same pattern as Task 3):

- **Trips.svelte**, label "Camera to browser":
  `GoPro (source, sub: 27MP interval)` -> `SQLite queue (store)` ->
  `EXIF + GPS (process)` -> `[Storage, stack]: SeaweedFS (store), NATS JetStream (store)`
  -> `imgproxy (process, sub: resize on the fly)` -> `Cloudflare CDN (external)`
  -> `trips.jomcgi.dev (output, sub: live WebSocket)`
- **Ships.svelte**, label "AIS pipeline":
  `AISStream.io (external)` -> "WebSocket" -> `ais-ingest (process)` ->
  `NATS JetStream (store)` -> "replay + subscribe" -> `ships-api (process)`
  -> `ships.jomcgi.dev (output, sub: MapLibre)`
- **Stargazer.svelte**, label "Scoring pipeline":
  `[Acquire, stack]: Light pollution (source), OSM roads (source), SRTM elevation (source), MET Norway (source)`
  -> `[Process, stack]: Dark regions (process), Road buffers (process), Zones (process)`
  -> `Composite score (output, sub: 0 to 100)`
- **Bazel.svelte**, label "One build graph":
  `[Anywhere, stack]: Laptop (source), CI (source), Agents (source)` ->
  `format (process)` -> `[Targets, stack]: Code (process), Helm manifests (process), apko images (process)`
  -> `BuildBuddy RBE (store, sub: remote cache)` -> `[Outputs, stack]: Git (output), Registry (output)`
- **RulesSemgrep.svelte**, label "Hermetic scanning":
  `PyPI wheels (source)` -> "extract" -> `semgrep-core (process, sub: OCaml binary)`
  -> `GHCR (store, sub: digest-pinned)` -> `Bazel tests (process, sub: 3 rule types)`
  -> `Pass / Fail (output, sub: 30s cached)`

**Step 2: Extend the registry** with the five new ids (`trips`, `ships`,
`stargazer`, `bazel`, `rules-semgrep`) and update the Task 3 comment.

**Step 3: Add registry coverage test** to `engineering-data.test.js`:

```js
import { diagrams } from "./diagrams/index.js";

it("every project has a diagram component", () => {
  for (const p of projects) {
    expect(diagrams[p.id], `missing diagram for ${p.id}`).toBeTruthy();
  }
});
```

Note: importing `.svelte` files in a vitest `node` environment works only
if the vitest config processes Svelte. Check `vitest.config.js`: it does
NOT load the Svelte plugin. So import the registry indirectly instead:
create `.../diagrams/registry-ids.js` exporting
`export const diagramIds = [...]` (plain strings, no Svelte imports), have
`index.js` build the component map and assert (dev-time) it covers
`diagramIds`, and have the test compare `diagramIds` against project ids.
Simplest concrete shape:

```js
// diagrams/registry-ids.js — plain-JS mirror of index.js keys, importable
// from vitest (node env) where .svelte files can't be parsed.
export const diagramIds = [
  "agent-platform",
  "knowledge-graph",
  "loom",
  "oci-model-cache",
  "sextant",
  "cloudflare-operator",
  "trips",
  "ships",
  "stargazer",
  "bazel",
  "rules-semgrep",
];
```

and in the test:

```js
import { diagramIds } from "./diagrams/registry-ids.js";

it("every project has a diagram registered", () => {
  for (const p of projects) {
    expect(diagramIds, `missing diagram for ${p.id}`).toContain(p.id);
  }
});
```

In `index.js`, import `diagramIds` and add a module-level sanity check so
the two files cannot drift silently:

```js
for (const id of diagramIds) {
  if (!diagrams[id]) throw new Error(`diagrams/index.js missing ${id}`);
}
```

**Step 4: Commit**

```bash
git add projects/monolith/frontend/src/routes/public/engineering/
git commit -m "feat(frontend): add engineering diagrams for apps and build tooling"
```

---

### Task 5: The page

**Files:**

- Create: `projects/monolith/frontend/src/routes/public/engineering/+page.svelte`

Complete implementation:

```svelte
<script>
  import { onMount } from "svelte";
  import { Footer, Sticker, Marquee } from "$lib/public/components";
  import { intro, marqueeItems, categories, projects } from "./engineering-data.js";
  import { diagrams } from "./diagrams/index.js";

  // Stable two-digit section numbers derived from roster order.
  const numbered = projects.map((p, i) => ({
    ...p,
    num: String(i + 1).padStart(2, "0"),
  }));

  const stickerColors = ["var(--accent)", "var(--blue)", "var(--coral)"];

  // Scroll-triggered reveals, mirroring the CV page's IntersectionObserver.
  onMount(() => {
    const observer = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (e.isIntersecting) {
            e.target.classList.add("in");
            observer.unobserve(e.target);
          }
        }
      },
      { threshold: 0.12 },
    );
    for (const el of document.querySelectorAll(".reveal")) {
      observer.observe(el);
    }
    return () => observer.disconnect();
  });
</script>

<svelte:head>
  <title>Joe McGinley — Engineering</title>
  <meta
    name="description"
    content="Engineering deep dives: agents, operators, data systems, and build tooling running on a bare-metal Kubernetes homelab."
  />
</svelte:head>

<div class="eng page">
  <!-- ═══ Hero ═══ -->
  <header class="hero">
    <div class="wrap hero-content">
      <p class="eyebrow">{intro.eyebrow}</p>
      <h1 class="display eng-title">{intro.title}</h1>
      <div class="hero-stickers">
        {#each intro.stickers as s, i}
          <Sticker color={stickerColors[i % stickerColors.length]} rotate={i % 2 ? 3 : -3}>
            {s}
          </Sticker>
        {/each}
      </div>
      <p class="lede">{intro.lede}</p>
      <a class="btn btn-secondary" href={intro.source.href} target="_blank" rel="noreferrer">
        {intro.source.label}
      </a>
    </div>
  </header>

  <Marquee items={marqueeItems} />

  <!-- ═══ Expo grid ═══ -->
  <section class="wrap expo" aria-label="Project index">
    {#each numbered as p, i}
      <a class="card-hard expo-card reveal" class:d1={i % 3 === 1} class:d2={i % 3 === 2} href={`#${p.id}`}>
        <div class="expo-card-top">
          <span class="expo-num mono">{p.num}</span>
          <span class="expo-tag mono" style:background={categories[p.category].color}>
            {categories[p.category].label}
          </span>
          {#if p.status}
            <span class="expo-tag mono expo-status">{p.status}</span>
          {/if}
        </div>
        <h2 class="expo-title">{p.title}</h2>
        <p class="expo-liner">{p.oneLiner}</p>
        <span class="expo-more mono">Deep dive ↓</span>
      </a>
    {/each}
  </section>

  <!-- ═══ Deep dives ═══ -->
  <div class="wrap dives">
    {#each numbered as p}
      {@const Diagram = diagrams[p.id]}
      <section class="dive reveal" id={p.id} aria-labelledby={`${p.id}-h`}>
        <div class="dive-head">
          <span class="dive-num mono">{p.num}</span>
          <h2 class="dive-title" id={`${p.id}-h`}>{p.title}</h2>
          <span class="dive-tag mono" style:background={categories[p.category].color}>
            {categories[p.category].label}
          </span>
          {#if p.status}
            <span class="dive-tag mono dive-status">{p.status}</span>
          {/if}
        </div>

        <div class="motivation">
          <span class="motivation-label mono">Motivation</span>
          <p>{p.motivation}</p>
        </div>

        {#if Diagram}
          <Diagram />
        {/if}

        <dl class="facts">
          {#each p.facts as f}
            <dt class="mono">{f.k}</dt>
            <dd>{f.v}</dd>
          {/each}
        </dl>

        {#if p.snippet}
          <pre class="snippet mono"><code>{p.snippet.code}</code></pre>
        {/if}

        {#if p.links?.length}
          <div class="dive-links">
            {#each p.links as l}
              <a
                class="btn btn-secondary"
                href={l.href}
                target={l.href.startsWith("/") ? undefined : "_blank"}
                rel={l.href.startsWith("/") ? undefined : "noreferrer"}
              >
                {l.label}
              </a>
            {/each}
          </div>
        {/if}
      </section>
    {/each}
  </div>

  <Footer />
</div>

<style>
  /* ═══ Hero ═══ */
  .hero {
    padding: 72px 0 56px;
    border-bottom: 2px solid var(--ink);
    background: var(--cream);
  }

  .hero-content {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 18px;
  }

  .eng-title {
    font-size: clamp(56px, 10vw, 120px);
  }

  .hero-stickers {
    display: flex;
    gap: 14px;
    flex-wrap: wrap;
  }

  .lede {
    max-width: 560px;
    font-size: 18px;
    color: var(--ink-2);
  }

  /* ═══ Expo grid ═══ */
  .expo {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
    gap: 24px;
    padding-top: 56px;
    padding-bottom: 56px;
  }

  .expo-card {
    display: flex;
    flex-direction: column;
    gap: 10px;
    padding: 20px;
  }

  .expo-card-top {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .expo-num {
    font-size: 12px;
    color: var(--ink-3);
    margin-right: auto;
  }

  .expo-tag {
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    border: 2px solid var(--ink);
    padding: 3px 8px;
    border-radius: 4px;
  }

  .expo-status {
    background: var(--paper);
  }

  .expo-title {
    font-family: var(--serif);
    font-weight: 400;
    font-size: 26px;
    line-height: 1.05;
  }

  .expo-liner {
    font-size: 14px;
    color: var(--ink-2);
    flex-grow: 1;
  }

  .expo-more {
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }

  /* ═══ Deep dives ═══ */
  .dives {
    display: flex;
    flex-direction: column;
    gap: 72px;
    padding-bottom: 96px;
  }

  .dive {
    scroll-margin-top: 80px;
    display: flex;
    flex-direction: column;
    gap: 22px;
  }

  .dive-head {
    display: flex;
    align-items: baseline;
    gap: 14px;
    border-bottom: 2px solid var(--ink);
    padding-bottom: 12px;
    flex-wrap: wrap;
  }

  .dive-num {
    font-size: 13px;
    color: var(--ink-3);
  }

  .dive-title {
    font-family: var(--serif);
    font-weight: 400;
    font-size: clamp(30px, 4.5vw, 44px);
    line-height: 1;
  }

  .dive-tag {
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    border: 2px solid var(--ink);
    padding: 3px 8px;
    border-radius: 4px;
    align-self: center;
  }

  .dive-status {
    background: var(--paper);
  }

  .motivation {
    background: var(--ink);
    color: var(--cream);
    border-radius: var(--radius);
    padding: 18px 20px;
    box-shadow: var(--shadow-hard-sm);
  }

  .motivation-label {
    display: block;
    font-size: 10px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    opacity: 0.7;
    margin-bottom: 6px;
  }

  .motivation p {
    font-size: 15px;
    line-height: 1.55;
  }

  .facts {
    display: grid;
    grid-template-columns: 180px 1fr;
    border: 2px solid var(--ink);
    border-radius: var(--radius);
    overflow: hidden;
    background: var(--paper);
  }

  .facts dt {
    padding: 12px 16px;
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.04em;
    border-bottom: 1px solid var(--rule);
    border-right: 2px solid var(--ink);
    background: var(--cream);
  }

  .facts dd {
    padding: 12px 16px;
    font-size: 14px;
    color: var(--ink-2);
    border-bottom: 1px solid var(--rule);
  }

  .facts dt:nth-last-of-type(1),
  .facts dd:nth-last-of-type(1) {
    border-bottom: none;
  }

  .snippet {
    border: 2px solid var(--ink);
    border-radius: var(--radius);
    background: var(--paper);
    box-shadow: var(--shadow-hard-sm);
    padding: 16px 18px;
    font-size: 13px;
    overflow-x: auto;
  }

  .dive-links {
    display: flex;
    gap: 14px;
    flex-wrap: wrap;
  }

  @media (max-width: 720px) {
    .facts {
      grid-template-columns: 1fr;
    }
    .facts dt {
      border-right: none;
      border-bottom: 1px solid var(--rule);
    }
    .dives {
      gap: 56px;
    }
  }
</style>
```

Implementation notes:

- `{@const Diagram = diagrams[p.id]}` capitalized local so Svelte 5 treats
  it as a component; render with `<Diagram />`.
- The `dt:nth-last-of-type(1)` trick removes the bottom rule of the final
  facts row; verify it survives the mobile single-column collapse (in one
  column the last `dd` is the true last element, the `dt` rule should stay,
  which the selector pair handles).
- Reveal animation classes (`reveal`, `d1`, `d2`) come from
  design-system.css; no local keyframes needed.

**Step 1:** Write the file as above.

**Step 2:** Commit:

```bash
git add projects/monolith/frontend/src/routes/public/engineering/+page.svelte
git commit -m "feat(frontend): add neo-brutalist engineering page"
```

---

### Task 6: Wire up navigation

**Files:**

- Modify: `projects/monolith/frontend/src/lib/public/components/Nav.svelte:9-20`
- Modify: `projects/monolith/frontend/src/routes/+layout.svelte:18-26`

**Step 1: Nav href.** In `Nav.svelte`, replace the engineering item and fix
the now-stale comment:

```js
// NOTES, CV, and ENGINEERING are same-host relative URLs so they resolve
// to public.jomcgi.dev/* from the public homepage and to
// private.jomcgi.dev/* from the private dashboard, without bouncing
// public visitors into the auth-gated private surface.
// HOME always points at the public site.
const publicItems = [
  { slug: "home", label: "HOME", href: "https://public.jomcgi.dev/" },
  { slug: "notes", label: "NOTES", href: "/notes" },
  { slug: "engineering", label: "ENGINEERING", href: "/engineering" },
  { slug: "cv", label: "CV", href: "/cv" },
];
```

**Step 2: Active route.** In `routes/+layout.svelte`, add one branch to
`activeRoute` (alongside the `/cv` check), and extend the comment's list:

```js
if (path === "/engineering" || path.startsWith("/engineering/")) {
  return "engineering";
}
```

**Step 3: Commit**

```bash
git add projects/monolith/frontend/src/lib/public/components/Nav.svelte \
  projects/monolith/frontend/src/routes/+layout.svelte
git commit -m "feat(frontend): point nav at the migrated engineering page"
```

---

### Task 7: Format, push, PR, CI, visual verification

**Step 1:** Run `format` from the worktree root. Commit any resulting
changes as `style: format engineering page sources` (only if it changed
files).

**Step 2:** Push and open the PR:

```bash
git push -u origin feat/engineering-page-migration
gh pr create \
  --title "feat(frontend): migrate engineering page to neo-brutalist SvelteKit route" \
  --body "$(cat <<'EOF'
Migrates jomcgi.dev/engineering (legacy Astro) to public.jomcgi.dev/engineering in the monolith frontend.

- New /engineering route: expo card grid + 11 deep-dive sections
- Content refreshed against the repo and CV: adds Agent Platform, Knowledge Graph, and Loom (pre-alpha); dissolves the stale Self-Hosted AI Stack section into the first two
- Hand-built CSS/SVG diagram primitives replace Mermaid (no CDN dependency)
- Nav ENGINEERING item now points at the new route; Astro site left untouched

Design doc: docs/plans/2026-06-10-engineering-page-migration-design.md

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

**Step 3:** Watch CI: `gh pr checks <number> --watch`. On failure, read
the log via `mcp__buildbuddy__get_invocation` (selector: `commitSha`) then
`get_target` / `get_log`, quote the failing assertion verbatim, fix, push.

**Step 4: Visual verification** (allowed: dev server is not a test run).
From `projects/monolith/frontend/`: `pnpm install` if needed, then
`pnpm dev`, and screenshot `http://localhost:5173/public/engineering` at
desktop (1440px) and mobile (390px) widths. Check: expo grid wraps cleanly,
diagrams stack vertically on mobile, facts tables collapse to one column,
no em-dashes visible in copy, anchors from cards land on the right
sections. Share screenshots with Joe before merging; this PR does NOT
auto-merge, Joe eyeballs the page first.

**Step 5:** Do not merge. Hand back to Joe for visual sign-off.
