# Operators

Custom Kubernetes operators built for this cluster: controllers that watch a
CRD and reconcile cluster state to match it, following the conventions in
[`best-practices.md`](best-practices.md) (idempotent, level-based
reconciliation; finalizer lifecycle for cleanup on delete; least-privilege
RBAC; non-root containers; OpenTelemetry tracing on every phase change).

## Operators

| Operator                                        | Purpose                                                                                                                                                  |
| ----------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [`oci-model-cache/`](oci-model-cache/README.md) | Syncs ML models from HuggingFace into OCI registries via a `ModelCache` CRD, so inference workloads pull models the same way they pull container images. |

## Conventions

Each operator is a self-contained Go module: a `cmd/` entrypoint, `api/` CRD
type definitions, `internal/` reconciliation logic, and a `helm/` chart for
deploying the operator itself (distinct from the charts of the resources it
manages). New operators should follow this layout and the reconciliation,
error-handling, and security patterns in `best-practices.md` rather than
inventing new ones.
