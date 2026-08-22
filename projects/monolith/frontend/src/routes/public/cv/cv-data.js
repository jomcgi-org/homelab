// CV content, transcribed from the canonical public CV maintained in the
// private cv repo (public/cv.md there; only the public/ variant is ever
// mirrored here). Bullet strings keep the markdown inline syntax (**bold**,
// [text](url)); the page renders them via the tiny tokenizer in +page.svelte
// rather than pulling in a markdown dependency.
//
// Firecracker timing figures are imported from fcstory/metrics.js, the
// single source of truth for those numbers across the whole public site.

import {
  agentFirstModelCallMs,
  agentRestoreColdMs,
  semgrepColdStartSec,
  semgrepRestoreMs,
  semgrepScanSec,
} from "../../../lib/public/fcstory/metrics.js";
import { LOCATION } from "../../../lib/public/seo.js";

export const contact = {
  email: "joe@jomcgi.dev",
  linkedin: {
    label: "linkedin/jomcgi",
    href: "https://www.linkedin.com/in/jomcgi/",
  },
  github: { label: "github/jomcgi", href: "https://github.com/jomcgi" },
  location: LOCATION.short,
};

export const name = "Joe McGinley";

export const tagline =
  "Senior Platform Engineer @ Semgrep · AWS / EKS · Kubernetes · eBPF";

export const summary =
  "Mostly I build infrastructure and abstractions so that other engineers don't have to think about it: Kubernetes, eBPF, and the layers under them. Caremad about developer and user experience.";

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
      "Run scan execution on Argo Workflows at scale: long-lived, latency-critical workloads kept within the control plane's etcd and apiserver limits.",
      "Migrated the org's ingress off deprecated ingress-nginx to Envoy Gateway on the Kubernetes Gateway API, with Cilium as the eBPF CNI and Linkerd as the L7 mesh.",
      "Built per-customer infrastructure cost attribution: usage metered at the kernel with eBPF, reconciled to the AWS bill.",
    ],
  },
  {
    company: "BenchSci",
    title: "Senior Software Engineer / SSE2",
    dates: "Oct 2022 – May 2025",
    blurb:
      "Pharma R&D SaaS. Three years building the infrastructure other engineers deployed on. Promoted to SSE2 within 12 months.",
    highlights: [
      {
        title: "Event-Driven Data Platform",
        intro:
          "BenchSci turns 25M+ scientific papers into structured biomedical knowledge through NER, LLM extraction, and knowledge-graph linking. Document processing was a weekly batch with pipeline-granularity caching: data freshness capped what the product team could ship, and unchanged work was reprocessed every cycle.",
        bullets: [
          "**Rebuilt it event-driven on GKE**: per-document events, per-document caching, scale-to-zero, so only changed documents reprocess. Processing went from weeks to minutes, cost dropped **from $644 to $69 per 2.5M docs (−89%)**, and the platform scaled to 100K+ concurrent documents with the full 25M+ corpus reprocessable on demand.",
          "**Made it the default**: for the ML researchers using it, adoption was a decorator and a Python function. The framework owned deployment, event semantics, the message log, and live data testing. **~200 engineers adopted it** as the data platform's foundation.",
        ],
      },
      {
        title: "Also at BenchSci",
        bullets: [
          "**OpenTelemetry org-wide adoption**: every team was rolling their own logs, metrics, and tracing. I drove a company-wide OTel rollout that gave every service all three by default. Incident **time-to-identify dropped from 94 to 23 minutes (−75%)** and false-alert volume fell 15%.",
          "**GPU inference at cloud-quota limits**: ran in-pod L4 inference for ~50 in-house ML models (paper extraction, entity enrichment, vision ML) inside the same event-driven pipeline, autoscaling across regions against a **~25K L4-GPU quota** and bursting onto spot capacity when available. Each document's work stayed in one region to avoid cross-region transfer.",
          "**Fixed the post-incident process**: it was bespoke per team. I wrote the company RCA playbook and led a cross-functional squad through it. **Time-to-resolution fell 40%** and the recurring SLA violations stopped.",
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
      "dbt / BigQuery modernization for a UK home-warranty business; ER model optimization; resilience work on a legacy platform.",
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
      "Led an on-prem to GCP migration; mentored junior engineers. Role eliminated in COVID restructuring.",
  },
];

export const earlierCareer =
  "**Chubb (2018–2020)**: account servicing for multinational corporate insurance; the final year was engineering: process automation, data science & analytics. Automated the majority of regional account-servicing admin and won two UK-wide hackathons, including an ML fraud classifier for claims handlers.";

export const personalIntro =
  "All of this runs on a bare-metal K3s cluster I operate at home, deployed with GitOps. Source: [github.com/jomcgi/homelab](https://github.com/jomcgi/homelab).";

export const projects = [
  `**EmberVM, a Firecracker agent substrate** ([projects/embervm](https://github.com/jomcgi/homelab/tree/main/projects/embervm)): a serverless invoke substrate on hardware-isolated **Firecracker microVMs**. A stateless node daemon restores a copy-on-write warm-base snapshot (**${agentRestoreColdMs}ms restore, ~${agentFirstModelCallMs}ms cold start to first model call**) and reverse-proxies HTTP over vsock into the guest; workloads are declarative Helm values, and durable state lives outside the VM. The guest never holds a real secret: a TLS-terminating egress proxy swaps placeholder tokens for the real credential at the network hop, so sandboxed code uses a GitHub token it can never read or exfiltrate.`,
  `**Semgrep scans on agent diffs** ([projects/firecracker/semgrep](https://github.com/jomcgi/homelab/tree/main/projects/firecracker/semgrep)): a snapshot-warm guest with a resident Semgrep engine scans the code the agents write, synchronously: **~${semgrepRestoreMs}ms restore, ~${semgrepScanSec}s for a full scan** with taint analysis against a ~${semgrepColdStartSec}s cold start, so the scanner sits inside the agent's write path instead of a CI queue.`,
  "**Grimoire** ([jomcgi.dev/app/grimoire](https://jomcgi.dev/app/grimoire)): takes scanned tabletop sourcebook pages and turns them into a domain knowledge graph you can ask questions of. An LLM extraction pipeline (a hosted frontier model, or my own in-cluster Qwen) fills Postgres + pgvector with **~37k entities and ~48k relationships across 35 books**, extracted off-pod as bounded, idempotent Argo CronWorkflows so nothing heavy touches the request path. On top: a per-campaign reader, an entity browser, and a rate-limited public RAG chat that **cites the exact source chunks** behind every answer. Access control is one visibility predicate every read path routes through, so the DM's secrets stay secret.",
  "**Local inference & autonomous maintenance**: llama.cpp serving **Qwen3.8-27B on a single consumer RTX 4090** (4-bit GGUF weights, q8 KV-cache); an MCP gateway in front of a self-built MCP server; and scheduled Claude routines doing autonomous platform maintenance over a Postgres + pgvector knowledge graph.",
  "**OCI Model Cache Operator** ([projects/operators/oci-model-cache](https://github.com/jomcgi/homelab/tree/main/projects/operators/oci-model-cache)): a Go operator that streams HuggingFace models into an OCI registry and rewrites pod volumes at admission, so pods mount models like container images. Sealed-interface state machines make invalid phase transitions a compile error.",
  "**Platform plumbing**: five custom Bazel rulesets, notably rules_helm, a rules_semgrep running the pinned scan engine directly for hermetic diff scans **from 2 minutes to 30 seconds**, and a WIP rules_ocaml making OCaml a first-class Bazel language; Argo CD GitOps; Envoy Gateway / Gateway API ingress behind a Cloudflare Tunnel (no open ports); Cilium (eBPF CNI, network policy, WireGuard), Kyverno, 1Password Operator, self-hosted SigNoz.",
  "**loom** (pre-alpha, with a collaborator, to be open-sourced): a take on **Palantir Foundry**, a typed-object data platform with built-in lineage and governance, on a **Rust** + DataFusion + DuckLake core. Postgres is the only stateful coordinator, so a new dataset, domain, or transform adds **no new system to run**.",
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
      "Metabase",
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
      "TypeScript",
      "SQL",
      "Starlark",
      "FastAPI",
      "SvelteKit",
      "Firecracker / microVMs",
      "vLLM",
      "MCP",
      "Agent orchestration",
      "Claude Code",
    ],
  },
];
