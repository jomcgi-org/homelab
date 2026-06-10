// CV content — transcribed from the canonical public CV maintained in the
// private cv repo (public/cv.md there; only the public/ variant is ever
// mirrored here). Bullet strings keep the markdown inline syntax (**bold**,
// [text](url)); the page renders them via the tiny tokenizer in +page.svelte
// rather than pulling in a markdown dependency.

export const contact = {
  email: "joe@jomcgi.dev",
  linkedin: {
    label: "linkedin/jomcgi",
    href: "https://www.linkedin.com/in/jomcgi/",
  },
  github: { label: "github/jomcgi", href: "https://github.com/jomcgi" },
  location: "Vancouver",
};

export const name = "Joe McGinley";

export const tagline =
  "Senior Platform Engineer @ Semgrep · AWS / EKS · Kubernetes · eBPF";

export const summary =
  "I run Kubernetes hands-on: from ingress to eBPF, controllers and all the CRDs in between. Caremad about developer & user experience.";

export const jobs = [
  // Current role stays deliberately scope-level: what the job is, not a
  // case-study of recent wins. The deep, metric-framed writeups live on
  // past roles only; the full current-role story stays in the PDF CV that
  // gets handed out directly.
  {
    company: "Semgrep",
    title: "Senior Platform Engineer",
    dates: "May 2025 – Present",
    blurb:
      "AWS / EKS / Kubernetes platform across 25 clusters for an AppSec SaaS serving large enterprise customers.",
    bullets: [
      "Run scan execution on Argo Workflows at scale: long-lived, latency-critical workloads held inside the control plane's etcd and apiserver limits.",
      "Migrated the org's ingress off deprecated ingress-nginx to Envoy Gateway on the Kubernetes Gateway API, with Cilium as the eBPF CNI and Linkerd as the L7 mesh.",
      "Built per-customer infrastructure cost attribution: usage metered at the kernel with eBPF, reconciled to the AWS bill.",
    ],
  },
  {
    company: "BenchSci",
    title: "Senior Software Engineer / SSE2",
    dates: "Oct 2022 – May 2025",
    blurb:
      "Pharma R&D SaaS. Three years removing complexity for other engineers — making the hard infra path the easy one, with business value as the byproduct. Promoted to SSE2 within 12 months.",
    highlights: [
      {
        title: "Event-Driven Data Platform",
        kicker: "Weeks to minutes, $644 → $69 per 2.5M docs",
        intro:
          "BenchSci turns 25M+ scientific papers into structured biomedical knowledge via NER, LLM extraction, and knowledge-graph linking. Document processing was a weekly batch with pipeline-granularity caching: freshness capped what product could ship, and unchanged work was reprocessed every cycle.",
        bullets: [
          "**Rebuilt it event-driven on GKE** — per-document events, per-document caching, scale-to-zero; only changed docs reprocess. Processing collapsed weeks → minutes, cost dropped **$644 → $69 per 2.5M docs (−89%)**, scaling to 100K+ concurrent documents with the full 25M+ corpus reprocessable on demand.",
          "**Made it the path of least resistance** — for the ML researchers who used it, adoption was a decorator and a Python function; the framework owned deployment, event semantics, the message log, and live data testing. **~200 engineers adopted it** as the data platform's canonical foundation.",
        ],
      },
      {
        title: "Also at BenchSci",
        bullets: [
          "**OpenTelemetry org-wide adoption** — every team was rolling their own logs, metrics, and tracing. I drove the company-wide OTel rollout: logs, metrics, and traces out of the box, one standard, one place to look. Incident **time-to-identify (TTI) dropped from 94 to 23 minutes (−75%)**; false-alert volume down 15%.",
          "**GPU inference at cloud-quota limits** — ran in-pod L4 inference for ~50 in-house ML models (paper extraction, entity enrichment, vision ML) inside the same event-driven pipeline, autoscaling across regions against a **~25K L4-GPU quota** — bursting onto spot capacity when available — with each document's work colocated in-region to avoid cross-region transfer.",
          "**RCA squad lead** — post-incident process was bespoke per team. I authored the company RCA playbook and led a cross-functional squad through it. **40% time-to-resolution reduction**; recurring SLA violations eliminated.",
        ],
      },
    ],
  },
  {
    company: "Ensono",
    title: "Platform Engineering Consultant (contract)",
    dates: "May 2022 – Oct 2022",
    blurb:
      "Greenfield GCP data platform for a hotel chain; Cloud Composer pipelines, IaC, BigQuery modeling.",
  },
  {
    company: "Hometree",
    title: "Senior Platform Engineer",
    dates: "Sep 2021 – May 2022",
    blurb:
      "DBT / BigQuery modernization for a UK home-warranty business; ER model optimization; resilience work on a legacy platform.",
  },
  {
    company: "AXA",
    title: "Senior Platform Engineer",
    dates: "Jan 2021 – Sep 2021",
    blurb:
      "Azure ETL + Terraform IaC; ML infrastructure that contributed to a **40% reduction in customer acquisition cost**.",
  },
  {
    company: "Sky",
    title: "Platform Engineer",
    dates: "Feb 2020 – Jan 2021",
    blurb:
      "Led an on-prem → GCP migration; mentored junior engineers. Role eliminated in COVID restructuring.",
  },
];

export const earlierCareer =
  "**Chubb (2018–2020)** — account servicing for multinational corporate insurance. Automated the majority of regional account-servicing admin and won two UK-wide hackathons (incl. an ML fraud classifier taken to production) — the start of the pivot into engineering.";

export const personalIntro =
  "All of this runs on a bare-metal K3s cluster I operate at home, GitOps-deployed. Source: [github.com/jomcgi/homelab](https://github.com/jomcgi/homelab).";

export const projects = [
  "**OCI Model Cache Operator** ([projects/operators/oci-model-cache](https://github.com/jomcgi/homelab/tree/main/projects/operators/oci-model-cache)) — Go operator + ModelCache CRD + admission webhook that streams HuggingFace models into an OCI registry and rewrites pod volume refs at admission, so pods mount models like container images. Sealed-interface Go state machines make invalid phase transitions a compile error.",
  "**Self-hosted agent platform** ([projects/agent_platform](https://github.com/jomcgi/homelab/tree/main/projects/agent_platform)) — vLLM inference, an MCP gateway, a sandboxed-execution orchestrator, and scheduled Claude routines running autonomous platform maintenance over a Postgres + pgvector knowledge graph.",
  "**Platform plumbing** — four custom Bazel rulesets (notably rules_helm, and a rules_semgrep that extracts the scan engine from its PyPI wheels for hermetic **30s-vs-2min** diff scans); Argo CD GitOps; Envoy Gateway / Gateway API ingress behind a Cloudflare Tunnel (no open ports); Linkerd, Kyverno, 1Password Operator, self-hosted SigNoz.",
];

export const collabAside =
  "Separately, a collaborative project (not part of the homelab):";

export const collabProjects = [
  '**loom** (in progress, with a collaborator under the weave-hand org — repo currently private, to be open-sourced) — a data platform built on one bet: **operating a governed, typed-object platform should scale sub-linearly with your data and your org** — a new dataset, domain, or transform adds no new system to run. Built clean-sheet in **Rust**, treating data governance as a safety property: an LLM-assisted STPA hazard analysis where "unsafe" means a governance violation, not a crash.',
];

export const skills = [
  {
    label: "Stack",
    items: [
      "AWS (EKS, S3)",
      "GCP (GKE, BigQuery, Pub/Sub)",
      "Azure",
      "Kubernetes",
      "eBPF",
      "Cilium",
      "Linkerd",
      "Gateway API",
      "Argo CD",
      "Terraform",
      "Bazel",
    ],
  },
  {
    label: "Data & Observability",
    items: [
      "Snowflake",
      "SQLMesh",
      "dbt",
      "Postgres (PGvector/HNSW)",
      "Neo4j",
      "Iceberg",
      "OpenTelemetry",
      "Prometheus",
      "Grafana",
      "SigNoz",
      "Honeycomb",
    ],
  },
  {
    label: "Languages & AI Infra",
    items: [
      "Go",
      "Python",
      "Rust",
      "SQL",
      "Starlark",
      "vLLM",
      "MCP",
      "Agent orchestration",
      "Claude Code",
    ],
  },
];
