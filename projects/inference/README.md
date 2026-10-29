# Inference

This chart configures the hub's CPU embeddings service. It runs llama.cpp with
Voyage 4 Nano, exposed inside the cluster as `inference-embeddings:8080`.
The hub Application combines [base values](deploy/values.yaml) with the
[GKE overlay](deploy/values-gke.yaml). See the
[Application definition](../gke-apps/inference/application.yaml) and
[platform architecture](../platform/ARCHITECTURE.md) for deployment ownership.

## Embeddings

The [Deployment](deploy/templates/deployment-embeddings.yaml) and
[Service](deploy/templates/service-embeddings.yaml) share the embeddings
selector. The container discovers a `.gguf` under `/model-image` and starts
`/app/llama-server` with `--embedding`, alias `voyage-4-nano` and zero GPU
layers. The model is an OCI image volume. The chart retains the 1Password
image-pull integration for that image.

The GKE overlay selects the serving pool and tolerates its brick taint. It
requests one CPU and 2 GiB of memory with a 12 GiB memory limit. The base
values retain the historical hostname selector and resource defaults for
render compatibility; the hub overlay supplies the deployed placement.
The container's startup, readiness and liveness probes use `/health`.
Its existing root user setting is unchanged; changing that requires separate
validation of the upstream image.

The hub configurations for [monolith](../monolith/deploy/values-gke.yaml),
[monolith-public](../monolith-public/deploy/values-gke.yaml) and
[EmberVM egress](../embervm/deploy/values-gke.yaml) reference the embeddings
service. Monolith's [shared embedding client](../monolith/shared/embedding.py)
uses the configured embeddings URL.

## Generative inference

The unused NInfer Deployment, generative Service, benchmark NodePort and
private HTTPRoute were retired under #7076. This chart provides no generative
endpoint or public ingress. Historical benchmark results and the experimental
self-hosted model registry remain in [model-bench](../model-bench/README.md).
Their presence does not establish current hardware availability or a reachable
tailnet service.

Pi uses Meta Spark through its [runtime shim](../embervm/runtimes/claude/shim.py).
Its local context, output and compaction limits are tested independently of
this chart. Monolith's hub overlay also selects Meta Spark for generative
requests. This change does not alter either provider route.

## Validation

Render the supported value stacks locally:

```bash
helm template inference projects/inference/deploy -n inference
helm template inference projects/inference/deploy -n inference \
  -f projects/inference/deploy/values-gke.yaml
```

[Chart regressions](deploy/chart_render_test.py) assert the embeddings resource
set, service contract, CPU arguments, model mount, probes, placement and
security settings. Both value stacks remain registered for schema
admissibility checks. Disabling embeddings leaves only the optional pull
secret integration.

After a separately authorized merge, an operator must verify the chart-version
write-back and inference
Application rollout, absence of retired resources, embeddings readiness and
an existing consumer's health. Repository renders alone do not establish live
acceptance.
