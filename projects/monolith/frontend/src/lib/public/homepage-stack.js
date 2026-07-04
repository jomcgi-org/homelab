// Hand-curated content for the front-page project stack. This is the single
// source of truth for what the homepage says we run. Add new systems here.
// kind: "projects" items render as story cards; kind: "strip" items render
// as labeled chips (linked when href is set). Story cards are the systems
// built here; the public apps are a strip of live links on top.

const GH = "https://github.com/jomcgi/homelab/tree/main";

export const stack = [
  {
    id: "apps",
    label: "APPS · LIVE",
    kind: "strip",
    items: [
      { name: "Ships", href: "/app/ships" },
      { name: "Stars", href: "/app/stars" },
      { name: "Hikes", href: "/app/hikes" },
      { name: "Campsites", href: "/app/campsites" },
      { name: "Trips", href: "/app/trips" },
      { name: "Dr Jobs", href: "/app/dr-jobs" },
      { name: "WC 2026", href: "/app/wc2026" },
      { name: "Chat", href: "/chat" },
      { name: "Docs", href: "/docs" },
    ],
  },
  {
    id: "systems",
    label: "SYSTEMS",
    kind: "projects",
    items: [
      {
        id: "agents",
        name: "AGENT PLATFORM",
        blurb: "Coding agents in hardware-isolated Firecracker microVMs.",
        engineering:
          "Snapshot-restored sandboxes with vsock-only egress and secret swapping at the boundary; agent recipes are compiled at runtime by a planner model.",
        tags: ["Firecracker", "Go", "MCP"],
        links: {
          docs: "/docs/agents",
          code: `${GH}/projects/firecracker`,
        },
      },
      {
        id: "monolith",
        name: "MONOLITH",
        blurb: "One deployable that serves everything on this domain.",
        engineering:
          "SvelteKit and Python behind split public and private tiers, Atlas-managed Postgres migrations, and a dozen apps riding a single GitOps rollout, including this page.",
        tags: ["SvelteKit", "Python", "Postgres"],
        links: {
          docs: "/docs/services",
          code: `${GH}/projects/monolith`,
        },
      },
      {
        id: "knowledge",
        name: "KNOWLEDGE GRAPH",
        blurb: "A fileless knowledge graph that gardens itself.",
        engineering:
          "Raw captures decomposed into atomic notes by scheduled agents; bodies in Postgres, embeddings for RAG, no files anywhere.",
        tags: ["Postgres", "Embeddings", "Agents"],
        links: {
          live: "/app/notes",
          code: `${GH}/projects/monolith/knowledge`,
        },
      },
      {
        id: "gitops",
        name: "GITOPS PIPELINE",
        blurb: "Every change lands by git push; ArgoCD does the rest.",
        engineering:
          "Bazel builds dual-arch apko images on remote executors, pins digests into OCI Helm charts, and ArgoCD syncs the cluster from the repo.",
        tags: ["ArgoCD", "Bazel", "apko"],
        links: {
          docs: "/docs/contributing",
          code: "https://github.com/jomcgi/homelab",
        },
      },
      {
        id: "observability",
        name: "OBSERVABILITY",
        blurb: "Traces, logs, and metrics for everything, self-hosted.",
        engineering:
          "OTEL instrumentation meshed through Linkerd into SigNoz on ClickHouse, with SLO rollups snapshotted into Postgres.",
        tags: ["SigNoz", "OTEL", "ClickHouse"],
        links: {
          docs: "/docs/observability",
        },
      },
      {
        id: "inference",
        name: "INFERENCE",
        blurb: "Local LLM serving on a single consumer GPU.",
        engineering:
          "vLLM serving a Qwen MoE plus embedding models, shared by chat, the agents, and the knowledge graph's RAG loop.",
        tags: ["vLLM", "Qwen", "GPU"],
        links: {
          code: `${GH}/projects/inference`,
        },
      },
    ],
  },
  {
    id: "platform",
    label: "PLATFORM",
    kind: "strip",
    items: [
      { name: "ArgoCD" },
      { name: "Linkerd" },
      { name: "SigNoz" },
      { name: "Envoy Gateway" },
      { name: "1Password Operator" },
      { name: "Atlas" },
    ],
  },
  {
    id: "compute",
    label: "COMPUTE",
    kind: "strip",
    items: [
      { name: "Kubernetes" },
      { name: "Firecracker" },
      { name: "Longhorn" },
      { name: "SeaweedFS" },
      { name: "vLLM on GPU" },
    ],
  },
  {
    id: "metal",
    label: "METAL",
    kind: "strip",
    items: [
      { name: "4 nodes" },
      { name: "1 GPU" },
      { name: "Cloudflare edge" },
    ],
  },
];

// Ordered labels for the typed card links.
export const LINK_LABELS = [
  ["live", "Visit live"],
  ["docs", "Read the docs"],
  ["code", "Read the code"],
];
