# SPIRE

This wrapper installs the SPIRE server, agent, CSI driver, controller manager, and CRDs.
Its workload identity design is recorded in ADR embervm/041 and tracked by #5706.
Phase 1 establishes the identity control plane but registers no workload consumer.
Before rollout, create the `k8s-homelab/spire-db` 1Password item with a URL-safe alphanumeric password.
Also create the CNPG basic-auth Secret by following [the datastore credential procedure](../../monolith/deploy/spire-db-secret.md).
Cluster-specific scheduling and storage settings belong in `values-<cluster>.yaml`.
