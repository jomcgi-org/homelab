# Operators

This directory retains one Kubernetes operator, `oci-model-cache`, with no
deployment after the home cluster was retired. Its controller watches
`ModelCache` resources and reconciles sync Jobs. It follows the reconciliation,
error-handling and security patterns in [`best-practices.md`](best-practices.md).

## Operators

| Operator                                        | Purpose                                                                                                                                                  |
| ----------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [`oci-model-cache/`](oci-model-cache/README.md) | Syncs ML models from HuggingFace into OCI registries via a `ModelCache` CRD, so inference workloads pull models the same way they pull container images. |

## Conventions

The operator belongs to the repository's root Go module. It has a `cmd/`
entrypoint, `api/` CRD type definitions, `internal/` reconciliation logic and
a `helm/` chart.

Sync Jobs have controller owner references to their `ModelCache`. Kubernetes
garbage collection removes those Jobs when the owning resource is deleted.
The TTL sweeper deletes expired `ModelCache` resources using `spec.ttl`;
absent or non-positive TTLs do not expire. The operator has no finalizers and
does not delete cached artifacts from the OCI registry.
