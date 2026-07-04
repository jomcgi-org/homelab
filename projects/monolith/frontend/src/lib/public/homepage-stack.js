// Hand-curated content for the front-page project stack. This is the single
// source of truth for what the homepage says we run. Add new apps here.
// kind: "projects" items render as story cards; kind: "strip" items render
// as plain labeled chips. Story cards are only for things built in this repo,
// not things merely run here.

const GH = "https://github.com/jomcgi/homelab/tree/main";

export const stack = [
  {
    id: "apps",
    label: "APPS",
    kind: "projects",
    items: [
      {
        id: "ships",
        name: "SHIPS",
        blurb: "Live AIS map of ship traffic off the BC coast.",
        engineering:
          "CDN-cached SSR snapshots over daily-partitioned Postgres, with a GPU heatmap of every voyage ever seen.",
        tags: ["SvelteKit", "Postgres", "MapLibre"],
        links: { live: "/app/ships", readme: `${GH}/projects/monolith/ships` },
      },
      {
        id: "stars",
        name: "STARS",
        blurb: "Stargazing forecast for anywhere in western Canada.",
        engineering:
          "Clear-dark-hours scoring on a 14,000-point grid, blended with CERRA cloud climatology and edge-cached for a year.",
        tags: ["SvelteKit", "Python", "ERA5/CERRA"],
        links: { live: "/app/stars", readme: `${GH}/projects/monolith/stars` },
      },
      {
        id: "hikes",
        name: "HIKES",
        blurb: "Trail catalog with conditions and weather.",
        engineering:
          "Seeded out-of-band to keep bulk data away from the GitOps migration path.",
        tags: ["SvelteKit", "Postgres"],
        links: { live: "/app/hikes", readme: `${GH}/projects/monolith/hikes` },
      },
      {
        id: "campsites",
        name: "CAMPSITES",
        blurb: "BC Parks campsite availability crossed with weather.",
        engineering:
          "Availability polling joined against forecast windows so a free weekend actually means a good weekend.",
        tags: ["SvelteKit", "Postgres"],
        links: {
          live: "/app/campsites",
          readme: `${GH}/projects/monolith/campsites`,
        },
      },
      {
        id: "trips",
        name: "TRIPS",
        blurb: "Shared trip planning and itineraries.",
        engineering:
          "Folded from a standalone service into the monolith to cut operational surface.",
        tags: ["SvelteKit", "Postgres"],
        links: { live: "/app/trips", readme: `${GH}/projects/monolith/trips` },
      },
      {
        id: "dr-jobs",
        name: "DR JOBS",
        blurb: "NHS Scotland anaesthetics vacancy aggregator.",
        engineering:
          "Scrapes and normalises health-board job feeds into one searchable view.",
        tags: ["Python", "Postgres"],
        links: {
          live: "/app/dr-jobs",
          readme: `${GH}/projects/monolith/dr_jobs`,
        },
      },
      {
        id: "wc2026",
        name: "WC 2026",
        blurb: "Scotland's World Cup 2026 odds, updated as results land.",
        engineering:
          "Elo-driven Monte Carlo simulation of the full tournament, re-run on every fixture.",
        tags: ["Python", "Monte Carlo"],
        links: {
          live: "/app/wc2026",
          readme: `${GH}/projects/monolith/worldcup`,
        },
      },
      {
        id: "chat",
        name: "CHAT",
        blurb: "Self-hosted LLM chat running on a local GPU.",
        engineering:
          "vLLM serving a Qwen MoE model, with tool use wired to the same MCP surface the agents use.",
        tags: ["vLLM", "Qwen", "MCP"],
        links: { live: "/chat", readme: `${GH}/projects/monolith/chat` },
      },
      {
        id: "knowledge",
        name: "KNOWLEDGE",
        blurb: "A fileless knowledge graph that gardens itself.",
        engineering:
          "Raw captures decomposed into atomic notes by scheduled agents; bodies in Postgres, embeddings for RAG, no files anywhere.",
        tags: ["Postgres", "Embeddings", "Agents"],
        links: {
          live: "/app/notes",
          readme: `${GH}/projects/monolith/knowledge`,
        },
      },
      {
        id: "agents",
        name: "AGENTS",
        blurb: "An agent platform with hardware-isolated sandboxes.",
        engineering:
          "Firecracker microVMs per session, vsock-only egress with token swapping, recipes compiled at runtime.",
        tags: ["Firecracker", "Go", "MCP"],
        links: { readme: `${GH}/projects/agent_platform` },
      },
      {
        id: "grimoire",
        name: "GRIMOIRE",
        blurb: "A Postgres-first D&D campaign manager.",
        engineering:
          "Long-form book content chunked and sequenced in Postgres with an omnibox over the lot.",
        tags: ["SvelteKit", "Postgres"],
        links: { readme: `${GH}/projects/monolith/grimoire` },
      },
      {
        id: "docs",
        name: "DOCS",
        blurb: "This repo's design docs and ADRs, published.",
        engineering:
          "Markdown indexed at build time and served by the same monolith that everything else runs on.",
        tags: ["SvelteKit", "Markdown"],
        links: { live: "/docs", readme: `${GH}/docs` },
      },
    ],
  },
  {
    id: "platform",
    label: "PLATFORM",
    kind: "strip",
    items: [
      { name: "ArgoCD", href: "https://github.com/argoproj/argo-cd" },
      { name: "Linkerd", href: "https://linkerd.io" },
      { name: "SigNoz", href: "https://signoz.io" },
      { name: "Envoy Gateway", href: "https://gateway.envoyproxy.io" },
      {
        name: "1Password Operator",
        href: "https://github.com/1Password/onepassword-operator",
      },
      { name: "Atlas", href: "https://atlasgo.io" },
    ],
  },
  {
    id: "compute",
    label: "COMPUTE",
    kind: "strip",
    items: [
      { name: "Kubernetes", href: "https://kubernetes.io" },
      { name: "Firecracker", href: "https://firecracker-microvm.github.io" },
      { name: "Longhorn", href: "https://longhorn.io" },
      { name: "SeaweedFS", href: "https://github.com/seaweedfs/seaweedfs" },
      { name: "vLLM on GPU", href: "https://github.com/vllm-project/vllm" },
    ],
  },
  {
    id: "metal",
    label: "METAL",
    kind: "strip",
    items: [
      { name: "4 nodes" },
      { name: "1 GPU" },
      { name: "Cloudflare edge", href: "https://www.cloudflare.com" },
    ],
  },
];
