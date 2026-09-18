# Inference

This domain declares the repository's generative inference and embeddings
workloads. The Helm chart and environment-specific ArgoCD inputs are separate:

```text
projects/inference/
├── chart/                 Helm metadata, defaults, templates, and dependencies
└── deploy/                Home Application, GKE values, and Kustomize entry point
```

The home Application and the GKE Application both read the chart from Git at
`targetRevision: HEAD`. The home composition uses the chart defaults. The GKE
composition layers `deploy/values-gke.yaml` over those defaults. This chart is
not published to the repository's OCI chart registry.

## Declared workloads

The chart defaults declare NInfer as the generative engine. It serves the
Qwen3.8-27B artifact under the stable `qwen3.6-27b` model identifier. The same
chart declares a CPU llama.cpp embeddings workload for voyage-4-nano. Detailed
engine arguments and capacity limits live beside the values they govern in
[`chart/values.yaml`](chart/values.yaml).

The GKE overlay disables the NInfer workload and keeps embeddings enabled. These
statements describe checked-in configuration. They do not verify current
cluster state.

## Consumers and routes

The monolith uses generation for chat, summarization, classification, and
vision paths. EmberVM's pi runtime uses the same OpenAI-compatible generation
API, with a test that keeps its advertised context window within the chart's
NInfer capacity. Knowledge and Grimoire retrieval use the embeddings Service.
The model-bench harness can target the generation API directly.

The chart declares an in-cluster Service and an HTTPRoute under `/llm` on the
private hostname. The route uses a 600-second request timeout and a Cloudflare
Access `SecurityPolicy`. Access policy configuration itself lives outside this
repository.

`benchMode.enabled` renames the generation Service and opens the configured LAN
NodePort for a benchmark session. That switch intentionally interrupts clients
using the stable in-cluster Service name. The embeddings Service stays in
place. Read the comments in [`chart/values.yaml`](chart/values.yaml) and
[`chart/templates/_helpers.tpl`](chart/templates/_helpers.tpl) before changing
it.

## Changing the chart

- Keep generic chart defaults in `chart/values.yaml` and cluster-specific
  overrides in `deploy/values-gke.yaml`.
- Update both checked-in Applications when a chart or values path moves.
- Keep the context-window guard in
  [`../embervm/runtimes/claude/pi_context_window_sync_test.py`](../embervm/runtimes/claude/pi_context_window_sync_test.py)
  pointed at the chart defaults.
- Render both the home and GKE compositions through the targets registered in
  [`chart/BUILD`](chart/BUILD).

Open engine work is tracked in
[#5471](https://github.com/jomcgi-org/homelab/issues/5471), which proposes a
separate small model for public chat. Its issue was open when this README was
written. It does not change the current chart.
