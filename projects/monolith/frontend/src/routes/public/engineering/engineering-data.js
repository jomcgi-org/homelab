// Engineering page content, the single source of truth for the expo grid
// and the deep-dive sections. Facts were checked against the repo and CV
// at migration time (2026-06). When a system changes, edit it here.
//
// Firecracker timing figures (ms literals) are NOT edited here: they are
// imported from fcstory/metrics.js, the single source of truth for those
// numbers across the whole public site.

import {
  agentRestoreColdMs,
  agentRestoreWarmMs,
  agentFirstModelCallMs,
} from "../../../lib/public/fcstory/metrics.js";

export const intro = {
  title: "Engineering",
  // Wonky sticker row under the headline. The systems-count sticker is
  // prepended on the page from projects.length so it never drifts; the
  // GitHub sticker is built from `source` below.
  stickers: ["Bare Metal K3s", "GitOps"],
  lede: "Deep dives into the systems on a bare-metal K3s cluster at home, deployed end-to-end with GitOps. Each covers why it exists and how it's built.",
  source: {
    label: "GitHub ↗",
    href: "https://github.com/jomcgi/homelab",
  },
};

export const marqueeItems = [
  "Go",
  "Rust",
  "Python",
  "Kubernetes Operators",
  "Firecracker microVMs",
  "vLLM on a 4090",
  "Postgres + pgvector",
  "Bazel + BuildBuddy",
  "ArgoCD GitOps",
  "Cilium",
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
      "Autonomous agents that do real platform work, each running in its own hardware-isolated Firecracker microVM, snapshotted to near-zero cost when idle and restored from memory in tens of milliseconds on the next turn.",
    motivation:
      "The first version ran unattended Claude agents in sandboxed Kubernetes pods. When Claude's terms of service changed around unattended agent use, that dispatch model no longer held. I split the execution substrate apart from the model choice and rebuilt the substrate on Firecracker: every agent request gets its own hardware-isolated microVM, paused and snapshotted when idle so it costs nothing, and restored in tens of milliseconds when woken. The durable pieces from v1 carried over: on-cluster inference, the MCP gateway, the knowledge-graph surface.",
    facts: [
      {
        k: "MicroVM per request",
        v: "Every agent request gets its own microVM with its own kernel, so code an agent writes runs behind a hardware boundary.",
      },
      {
        k: "Snapshot / restore",
        v: `An idle agent thread is paused and snapshotted (memory plus rootfs), releasing all compute, then restored on the next turn. Measured restore is ${agentRestoreColdMs}ms cold, ${agentRestoreWarmMs}ms warm; trigger to first model call is ~${agentFirstModelCallMs}ms. A thin Postgres-reconcile controller owns the lifecycle, porting E2B's open-source snapshot architecture onto the Firecracker primitive.`,
      },
      {
        k: "Secret-swap egress",
        v: "The guest is vsock-only and never holds a real credential. A TLS-terminating sidecar routes by SNI/Host and swaps a placeholder token for the real secret (a GitHub or model API key) at the network hop, so sandboxed code uses credentials it can never read or exfiltrate.",
      },
      {
        k: "Postgres control plane",
        v: "High-churn idle-agent state lives in Postgres, keeping thousands of waiting threads off the cluster control plane. The same registry backs the list and resume catalog exposed over MCP.",
      },
      {
        k: "Local inference",
        v: "vLLM serves a Qwen3.6 MoE (35B-A3B, int4) on a single RTX 4090 for routine work; frontier models are reached over the swapped egress only where the task warrants it.",
      },
    ],
    links: [
      {
        label: "projects/firecracker",
        href: "https://github.com/jomcgi/homelab/tree/main/projects/firecracker",
      },
    ],
  },
  {
    id: "embervm",
    category: "agents",
    title: "EmberVM",
    oneLiner:
      "A Firecracker microVM orchestrator: an Elixir control plane and a Go node daemon run one-shot tasks, bankable stateful sessions, and warm HTTP serving on the same substrate.",
    motivation:
      "fc-invoke proved the substrate but hardcoded one workload shape: a stateless invoke. EmberVM is the successor, built by forking the node daemon and putting a BEAM control plane in front of it. Semgrep scans already run on it, a public image renderer serves warm from it, and fc-invoke is frozen with the goose agent as its last tenant. The control plane owns placement and policy and stays off every serving hit path.",
    facts: [
      {
        k: "Workload classes",
        v: "task (one-shot, no network card, one channel to the host), session (a stateful sandbox, banked to disk when idle and relit on the next invoke), serving (a warm HTTP endpoint reachable from the edge).",
      },
      {
        k: "Serving data path",
        v: "The control plane hands out the routes; requests go from the edge into the VM without touching it or Kubernetes.",
      },
      {
        k: "Quotas and metering",
        v: "Fail-closed: a principal with quota 0 is hard-stopped at submit. Metering rides the operation itself, bills success and failure alike, and is queryable at /v1/usage.",
      },
      {
        k: "Public exposure",
        v: "One public route, scoped at three layers: the HTTPRoute pins host and path, the node Envoy exact-matches the internal authority, and the guest shim keeps its control endpoints unreachable. 120 requests/min at Envoy plus a 3600 vCPU-second daily quota.",
      },
      {
        k: "State",
        v: "A Postgres op-log: a 30-day journal with 7-day terminal-task retention. The current design is maintained in projects/embervm/ARCHITECTURE.md.",
      },
    ],
    links: [
      {
        label: "projects/embervm",
        href: "https://github.com/jomcgi/homelab/tree/main/projects/embervm",
      },
    ],
  },
  {
    id: "knowledge-graph",
    category: "agents",
    title: "Knowledge Graph",
    oneLiner:
      "An LLM pipeline that decomposes my notes into structured facts, embeds them, and serves semantic search, both to my agents and to this site's search bar.",
    motivation:
      "An on-cluster model breaks each of my notes into atomic facts and stores them so a question pulls back the right one.",
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
      { label: "Browse the notes", href: "/app/notes" },
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
      "Loom is a collaborative bet on a governed data platform where a new dataset, domain, or transform adds no new system to run. It treats governance as a safety property: the STPA hazard analysis defines unsafe as a governance violation.",
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
        v: "OpenLineage events plus STPA-derived constraints. The ontology and ACLs live next to the data they govern.",
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
        k: "Deduplication",
        v: "The HuggingFace baseModels API resolves derivative models to their base, so derivatives share OCI layers with the base repo.",
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
      "Every operator I wrote had the same bugs: invalid state transitions, forgotten error handling, missing metrics. Sextant defines Kubernetes operator state machines declaratively and generates the boilerplate for their reconciliation loops.",
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
        v: "Go expressions embedded in the YAML, evaluated at transition time. Invalid expressions fail at code-generation time.",
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
      "A dash-mounted GoPro becomes a browseable journey: server-side EXIF extraction, Postgres-backed trip points, on-the-fly image resizing, edge caching.",
    motivation:
      "I wanted an easy way to share trips with friends and family. A GoPro on the dash captures photos automatically, and my homelab turns them into a map they can follow day by day, or replay later. Works for anything with GPS-tagged photos.",
    facts: [
      {
        k: "Capture",
        v: "A Python asyncio controller drives the camera over WiFi: GPS-triggered interval capture, a persistent SQLite download queue, and exponential backoff on connection drops.",
      },
      {
        k: "Ingest",
        v: "Each geotagged frame is POSTed to a private endpoint gated by Cloudflare Access at the gateway (no app-level key). The server extracts EXIF, derives a trip point, and upserts it. The write path ships only in the private image, so it is unreachable from the internet.",
      },
      {
        k: "Store",
        v: "Postgres is the source of truth for trip points; folding trips into the monolith retired the old NATS event store. Image bytes live content-addressed in SeaweedFS.",
      },
      {
        k: "Delivery",
        v: "imgproxy resizes originals on the fly and Cloudflare caches at the edge.",
      },
      {
        k: "Display",
        v: "A read-only, SSR tier renders the public pages from localhost in-pod and is CDN-cached: MapLibre vector tiles with terrain hillshade, day-by-day galleries, and an elevation-profile scrubber. The public reads never touch the write path.",
      },
    ],
    links: [{ label: "Live trips", href: "/app/trips" }],
  },
  {
    id: "ships",
    category: "apps",
    title: "Ships: AIS Vessel Tracking",
    oneLiner:
      "Live maritime traffic on a map: AIS position reports streamed into the cluster, persisted to Postgres, and served as a CDN-cached MapLibre snapshot.",
    motivation:
      "Living near the coast, I wanted to see what ships are passing by in real time. AIS data is publicly broadcast by vessels, but there is no simple way to visualize it locally. This pipeline streams AIS data through my cluster onto a map.",
    facts: [
      {
        k: "AIS ingest",
        v: "A background task inside the monolith holds a websocket to AISStream.io, filters to a Pacific Northwest box, and writes position reports straight to Postgres.",
      },
      {
        k: "Storage",
        v: "Postgres is the single source of truth. ships.positions is range-partitioned by day. A stateless persister reads affected vessels back, dedups, and upserts a latest-positions serving table.",
      },
      {
        k: "Ships API",
        v: "SSR-only REST is reached from localhost in-pod: a snapshot endpoint for the initial render and a per-vessel track on click. Both are CDN-cached with ETag 304s.",
      },
      {
        k: "MapLibre UI",
        v: "Vessels render as a GPU GeoJSON symbol layer with directional icons by heading. A separate WebGL layer maps distinct-vessel traffic density per ~500m cell.",
      },
    ],
    links: [
      {
        label: "Live at jomcgi.dev/app/ships",
        href: "https://jomcgi.dev/app/ships",
      },
    ],
  },
  {
    id: "stargazer",
    category: "apps",
    title: "Stars: Dark-Sky Windows",
    oneLiner:
      "Finds the best dark-sky viewing windows across Scotland: scores each site's upcoming hours for darkness and clear sky, then serves the ones that qualify.",
    motivation:
      "Finding a good stargazing night means combining how dark a site is with whether the sky will actually be clear once it gets dark. Stars pairs a light-pollution grid of road-accessible dark sites with rolling weather forecasts and surfaces the upcoming hours that are both dark and clear.",
    facts: [
      {
        k: "Site grid",
        v: "A light-pollution grid of ~14k road-accessible dark sites is built offline and uploaded to SeaweedFS. A scheduled job wholesale-replaces the stars.sites table.",
      },
      {
        k: "Forecast scoring",
        v: "An hourly job fetches MET Norway forecasts for every site and scores each future hour for darkness (sun below the threshold, astronomy via astral) and clear sky (cloud below the threshold).",
      },
      {
        k: "Metric",
        v: "The unit is clear-dark hours. Qualifying hours land in Postgres (stars.site_hours); an hourly prune drops hours once their clock hour has elapsed.",
      },
      {
        k: "Delivery",
        v: "A wholly public, read-only domain folded into the monolith: a slim SSR payload lists sites with their upcoming windows, the per-site history graph loads lazily, and the page is edge-cached.",
      },
    ],
    links: [
      {
        label: "Live at jomcgi.dev/app/stars",
        href: "https://jomcgi.dev/app/stars",
      },
    ],
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
        v: "apko Alpine images from YAML with pinned lock files. One target declares the base, the layers and the push, with the architecture as a flag.",
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
