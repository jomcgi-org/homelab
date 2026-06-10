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
