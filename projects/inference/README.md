# Inference

On-cluster LLM serving for everything in this repo that calls a model and
does not want to pay per token: the monolith's Discord, chat, summarize,
vision and classifier callsites, monolith-dev (which points every provider
here), monolith-public retrieval, EmberVM pi-runtime qwen sessions, and the
`model-bench` harness. One GPU (the RTX 4090 on node-4), one generative
model, one CPU embeddings model.

This is the entry point for the domain. The detailed reasoning lives in the
values-file comments, which are deliberately long; this file says where to
look and what holds across the whole directory.

## What runs

| Workload | Engine | Model | Where |
| --- | --- | --- | --- |
| `inference` (LLM) | llama.cpp `server-cuda`, all layers in VRAM | Qwen3.8-27B, unsloth GGUF Q4_K_M with mmproj (vision) and MTP draft decoding, served under the alias `qwen3.6-27b` | node-4 GPU, `runtimeClassName: nvidia` |
| `inference-embeddings` | llama.cpp, `n-gpu-layers 0` (host RAM) | voyage-4-nano GGUF Q8_0, alias `voyage-4-nano` | node-4 CPU |

The served alias is pinned on purpose: 10+ callsites and the model-bench
leaderboard hardcode `qwen3.6-27b`, so a model change does not rename it.
Both models are OCI artifacts under `ghcr.io/jomcgi/models`, mounted as
`image:` volumes and auto-discovered by the container entrypoint
(`templates/deployment-llamacpp.yaml`, `templates/deployment-embeddings.yaml`).
They are pushed out of band with `hf2oci`; the image must exist in ghcr before
ArgoCD syncs or the pod sits in `ImagePullBackOff`.

**Decided direction (#5155, PR #5162):** the LLM engine is being replaced by
NInfer (sm_89 fork at `jomcgi/ninfer-4090`) as the only engine, deleting the
vLLM and llama.cpp templates rather than keeping them behind `llm.engine`.
When that merges, the engine rows above change; the consumer map, ingress and
bench mode below do not.

## Layout

```
projects/inference/
└── deploy/
    ├── application.yaml        ArgoCD Application (git source, targetRevision HEAD)
    ├── Chart.yaml              chart lives HERE, not in a sibling chart/
    ├── values.yaml             the domain's real documentation
    ├── templates/
    └── charts/cf-ingress-*.tgz vendored from projects/platform/cf-ingress-library
```

Two things about this layout are unlike the rest of `projects/`:

- **The chart sits inside `deploy/`.** Repo convention is `chart/` plus
  `deploy/`; this one predates it. Splitting it is a task on #4667 and is not
  done here. `deploy/BUILD` also exports `values.yaml` as a filegroup to
  `//projects/embervm/runtimes/claude`, whose context-window test reads it.
- **It deploys on merge, not on a chart publish.** `application.yaml` is a
  plain git-path source at `targetRevision: HEAD`, and `//bazel/images:push_charts`
  does not list this chart. So there is no OCI chart, no post-merge
  `chart-version-bot` write-back, and no Kargo: a merged change to this
  directory is live within ArgoCD's sync interval, the same way
  `projects/platform/*` behaves. `Chart.yaml` `version:` is decorative.

## Engine switch

`llm.engine` selects exactly one of `templates/deployment.yaml` (vLLM) and
`templates/deployment-llamacpp.yaml`. The unselected one is not rendered at
all, because both request `nvidia.com/gpu: 1` on a single card and a
scaled-to-zero second Deployment would still be dishonest about capacity.
Both Deployments carry the same name, selector and port, so the Service and
every caller URL survive a flip.

Today's value is `llamacpp`, recorded in `values.yaml` as a deliberate
temporary state: Qwen3.8-27B's only Ada-executable quantization is GGUF. The
vLLM block (`vllm.*`, `server.chatTemplate`, the `inference-chat-template`
ConfigMap and the `inference-vllm-cache` PVC) stays rendered or kept intact
for the one-line revert. The ConfigMap and PVC are therefore live objects
that nothing consumes while `llm.engine` is `llamacpp`; that is intended, and
#5162 removes them.

## Sizing: read before changing a number

`llamacpp.ctxSize` is the **total** KV pool shared across `llamacpp.parallel`
slots (98304 / 3 = 32768 per slot, the window every monolith callsite assumes),
not per-sequence like vLLM's `--max-model-len`. VRAM cost is not linear in
context; the values comment carries the measured points and the history of
the extrapolation that shipped a 656 MiB margin. Interpolate between measured
points, re-read `nvidia-smi`, never fit a line. The deployment is `Recreate`
on one GPU, so an over-committed pool is a crash loop, not a degraded pod.

Host memory follows ADR platform/010: the LLM pod requests steady-state and
limits at the real anonymous peak (`homelab-critical` priority); embeddings
runs requests == limits because llama.cpp holds its weights in host RAM.

Slot policy on the consumer side is in `projects/monolith/shared/inference.py`:
synchronous callers are uncapped, asynchronous jobs take one slot each
(`ASYNC_SLOT_BUDGET`), bounded per process rather than cluster-wide.

## Reaching it

| Caller | URL | Auth |
| --- | --- | --- |
| In-cluster OpenAI clients | `http://inference.inference.svc.cluster.local:8080/v1` | none (`api_key` ignored) |
| In-cluster embeddings | `http://inference-embeddings.inference.svc.cluster.local:8080` | none |
| Off-cluster OpenAI clients | `https://private.jomcgi.dev/llm/v1` | Cloudflare Access service token |
| Browser | `https://private.jomcgi.dev/llm/` (trailing slash is load-bearing) | Cloudflare Access login |

The external route is `templates/httproute-private.yaml`, written out by
hand rather than taken from the `cf-ingress` library because the library
emits no `timeouts` and Envoy's 15s default truncates a completion. Its
SecurityPolicy validates the Access JWT with `optional: true`, because a
service-token request clears Access without a `Cf-Access-Jwt-Assertion`
header; the template explains what that does and does not give up. Access is
configured in the Cloudflare dashboard, not in this repo; the `/llm`
path-scoped application REPLACES the hostname-wide one rather than layering
on it. Two clocks apply: Envoy's 600s route timeout here, and Cloudflare's
non-configurable 524 after 100s with no byte from the origin, so stream long
completions.

Why `private.jomcgi.dev` and not `friends.jomcgi.dev`: the friends lanes run
an Envoy `oidc` block that answers a headless client with a browser redirect.

In-cluster reachability for guests is a separate gate: EmberVM session
guests have no NIC, and `inference.inference.svc.cluster.local:8080` is an
explicit entry in the egress allowlist (`projects/embervm/chart/values.yaml`,
`egress.internal.allowlist`). The pi runtime hardcodes the same base URL in
`projects/embervm/runtimes/claude/shim.py` (`PiProcess.INFERENCE_BASE_URL`).

## Thinking control

Callers turn Qwen reasoning off per request with
`chat_template_kwargs: {enable_thinking: false}`; llama.cpp honours it,
`reasoning_effort` at the top level is ignored. Helpers live in
`projects/monolith/shared/inference.py` (`thinking_off`,
`reasoning_effort`). The pi lane defaults to thinking off
(`PI_DEFAULT_THINKING_LEVEL` in `shim.py`) because it is the small-task lane;
long tasks need it back on, tracked in #5051. Reasoning comes back in
`reasoning_content` (`--reasoning-format deepseek`), which is the shape the
summarizer and chat agents already parse from the vLLM era.

## Bench mode

`benchMode.enabled` is one boolean that starts and ends a benchmarking
session, and flipping it on is a **deliberate outage** for every in-cluster
qwen consumer: the LLM Service is renamed to `inference-bench` so the
hardcoded DNS name fails fast (NXDOMAIN, not a hang), llama.cpp drops to one
slot with the whole KV pool, and the Service becomes an unauthenticated LAN
NodePort (`30800`, the only NodePort in the cluster). The external `/llm/v1`
route follows the rename and keeps working; embeddings are untouched. Nothing
pages during a session: the qwen synthetic CronWorkflow is suspended and has
never run, and the composite `/health` carries no inference dependency.
`templates/_helpers.tpl` explains why a Service rename rather than
`fullnameOverride` or a NetworkPolicy.

## Observability

Both pods are scraped on the pod IP (`prometheus.io/scrape`), and llama.cpp
runs with `--metrics`. The external route also forwards `/metrics` and
`/props` under `/llm/`, behind Access. There is no alert on the LLM being
down (see bench mode above); retrieval breakage from embeddings shows up in
grimoire and knowledge search first.

## Decisions and outstanding work

No ADR is specific to this domain. The ones that touch it are about its
consumers and are harvested by other rollups:

| Decision | Status | Claimed by |
| --- | --- | --- |
| ADR platform/010, burstable memory and priority classes (sizing rule used above) | Accepted | platform rollup |
| ADR agents/012, 013, 021, 024 (model tiers, which lanes use the in-cluster model) | various | monolith rollup |
| ADR security/005 (public chat hardening, embeddings contention) | Accepted | monolith rollup |

Issues: #5155 / PR #5162 (NInfer as the only engine), #5051 (qwen3.8 and pi
lane efficiency loop, thinking per lane), #4667 (the `chart/` + `deploy/`
split).
