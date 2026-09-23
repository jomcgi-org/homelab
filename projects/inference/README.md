# Inference

This project serves generative inference from the home GPU and embeddings from
CPU. NInfer is the only generative engine. The base values run `ninfer-serve`
with Qwen3.8-27B on node-4 and run a separate llama.cpp embeddings workload
with voyage-4-nano.

The manifests describe desired state. They cannot establish current pod health,
model availability in the node cache, ArgoCD sync state, or the Cloudflare
Access policy held outside this repository. Check those live before an
operation that depends on them.

## What runs

| Workload | Server and model | Repository scope |
| --- | --- | --- |
| `inference` | NInfer `ninfer-serve`, Qwen3.8-27B from a `.ninfer` artifact | Enabled by [`deploy/values.yaml`](deploy/values.yaml) for the home deployment |
| `inference-embeddings` | llama.cpp on CPU, voyage-4-nano from a `.gguf` artifact | Enabled in the base values and the GKE overlay |

The NInfer container finds the first sorted `*.ninfer` file under
`ninfer.modelVolume.mountPath` and passes it to `ninfer-serve`. The Helm helper
builds the remaining arguments from `server.*` and `ninfer.*`, including the
model ID, context and KV capacity, concurrency, pending-request limits, MTP,
and vision. [`templates/deployment.yaml`](deploy/templates/deployment.yaml) and
[`templates/_helpers.tpl`](deploy/templates/_helpers.tpl) are the executable
configuration. The comments in [`values.yaml`](deploy/values.yaml) record the
measured sizing constraints.

Model files arrive as OCI image volumes. The referenced images must exist and
be readable before ArgoCD syncs the workload. A missing image leaves the pod in
`ImagePullBackOff`; a mounted volume without a `.ninfer` artifact makes the
container exit before starting the server.

### The public model ID stays stable

`ninfer.modelId` is pinned to `qwen3.6-27b`, while the current artifact is
Qwen3.8-27B. Callers send the pinned ID. Changing an artifact does not require
renaming every caller or splitting the benchmark history. The self-hosted
Qwen3.8 row in [`model-bench/models.yaml`](../model-bench/models.yaml) follows
the same rule through its `api_model` field.

Change the alias only with all callsites and benchmark records in scope. The
source references include monolith dev values, benchmark configuration, and
the private route documentation.

## Configured consumers

Repository configuration records these call paths:

| Consumer | NInfer relationship in current source |
| --- | --- |
| Monolith base values | [`monolith/deploy/values.yaml`](../monolith/deploy/values.yaml) injects the NInfer base URL as `LLAMA_CPP_URL`. Chat can fall back to it when no provider-specific base URL is selected; summarization, vision, and classification share the same seam. The GKE overlay replaces the value with Meta Spark, and the home cluster no longer enrolls monolith. |
| Monolith dev | [`monolith/dev/deploy/values.yaml`](../monolith/dev/deploy/values.yaml) points its brief compiler and Grimoire extraction settings at NInfer and uses `qwen3.6-27b`. Those paths retain their own enablement gates. |
| Public monolith | The base chart can use the service through `CHAT_PUBLIC_INFERENCE_URL`. [`monolith-public/deploy/values-gke.yaml`](../monolith-public/deploy/values-gke.yaml) replaces generation with Meta Spark while keeping the in-cluster embeddings service. |
| `model-bench` | The harness reaches any OpenAI-compatible `/v1` base URL on demand. Its self-hosted Qwen3.8 row sends the `qwen3.6-27b` API model ID. |

Closed [PR #5167](https://github.com/jomcgi-org/homelab/pull/5167) also listed
EmberVM Pi as a generative consumer. That entry is historical. Current
`PiProcess` uses Meta Spark. Its remaining direct NInfer dependency is a test
that keeps Pi's context window within `ninfer.maxContext` and the shared KV
capacity.

Embeddings have a different consumer set. Monolith, monolith-public, and
EmberVM's GKE egress allowlist retain the `inference-embeddings` service. The
GKE inference overlay disables NInfer and keeps this CPU workload enabled.

## Layout and delivery

```text
projects/inference/
├── README.md
└── deploy/
    ├── application.yaml
    ├── Chart.yaml
    ├── values.yaml
    ├── values-gke.yaml
    └── templates/
```

The Helm chart lives inside `deploy/`. The home cluster includes
[`deploy/application.yaml`](deploy/application.yaml), which reads this git path
at `targetRevision: HEAD` with the base values. The GKE
[`Application`](../gke-apps/inference/application.yaml) also reads `HEAD` and
applies `values-gke.yaml` over the base values. That overlay disables NInfer
and private ingress, then schedules embeddings in GKE.

These Applications reconcile source from git. A merge changes their desired
state without an OCI chart publication or chart-version write-back. The model
and server images are published separately.

## Reaching NInfer

In-cluster clients receive their base URL from chart values or environment
variables. Keep that configuration seam; do not add a service DNS name to
application code.

[`templates/httproute-private.yaml`](deploy/templates/httproute-private.yaml)
publishes the OpenAI-compatible base URL at
`https://private.jomcgi.dev/llm/v1`. The route rewrites that prefix to `/v1`
and allows 600 seconds for requests to the backend. Streaming avoids
Cloudflare's shorter no-byte timeout for long generations.

Cloudflare Access guards the hostname at the edge. Headless callers send
`CF-Access-Client-Id` and `CF-Access-Client-Secret`; their values live outside
the repository. The Envoy policy validates an Access JWT when Cloudflare sends
one. Service-token requests clear Access without that JWT, so the policy marks
JWT validation optional.

The broader `/llm/` rule forwards NInfer server paths such as `/health`,
`/metrics`, and `/slots`. NInfer has no browser UI. Access still covers those
paths.

## Bench mode

`benchMode.enabled` is the single switch for an isolated benchmark session. It
is disabled by default. Enabling it:

- renames only the generative Service to `inference-bench`, interrupting callers
  that use the stable in-cluster Service name;
- keeps the Deployment and its GPU claim in place;
- moves the private route to the renamed Service;
- opens the configured unauthenticated NodePort to the LAN;
- leaves the embeddings Service running.

Turn the switch off as soon as the session ends. The NodePort closes with it.
Anything on the LAN can use the GPU while the switch is on.

The benchmark harness can avoid the LAN port by forwarding the Deployment:

```sh
kubectl port-forward -n inference deploy/inference 18080:8080
cd projects/model-bench
python3 -m bench run --model qwen3.8-27b \
  --base-url http://127.0.0.1:18080/v1
```

Forward the Deployment because its name stays fixed in bench mode. Forwarding
the normal Service fails after the rename.

## Health and metrics

Kubernetes startup, liveness, and readiness probes call NInfer's `/health`.
Prometheus scrapes `/metrics` on the pod port.

Public `https://jomcgi.dev/health` is a separate composite check. The frontend
proxies it to the public backend's `/api/health`; the `chat_public` inference
component calls `/v1/models` on the configured `CHAT_PUBLIC_INFERENCE_URL`.
An unreachable endpoint or a non-2xx response is fatal and makes public health
return HTTP 503 with `inference` among the failing components. An unset URL
currently fails open.

The configured provider varies by environment. When that URL points to NInfer,
an NInfer outage makes public health fail. The GKE overlay currently points it
to Meta Spark, so the same fatal check covers that provider there.

## History

[PR #5162](https://github.com/jomcgi-org/homelab/pull/5162) replaced the former
generative serving stack with NInfer and removed the old engine-selection
configuration. The README proposed in closed PR #5167 described the earlier
stack. This page retains only topics verified against current source: consumer
configuration, alias pinning, git-path delivery, private ingress, bench mode,
and fatal public inference health.
