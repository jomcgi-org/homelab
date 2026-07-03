# ADR 037: Label-Driven Firecracker Node Enrollment

**Author:** jomcgi
**Status:** Accepted
**Created:** 2026-07-02
**Builds on:** [030 - fc-invoke Configurable Firecracker Surface](030-fc-invoke-configurable-firecracker-surface.md) (the node daemon this places), [031 - Control-Plane / Data-Plane Split](031-cluster-node-control-data-plane-split.md) (which scopes the DaemonSet + cross-node placement as multi-node future work), [033 - Golden-Template Distribution](033-golden-template-distribution-daemon-pulled-oci.md) (the per-ISA template that bounds how portable a node's snapshots are)

---

## Problem

Standing up a Firecracker-capable node was a manual, node-4-specific ritual: five `sudo bash` scripts under `projects/platform/firecracker-node/` (devmapper thin-pool, kata install to `/opt/kata`, containerd rewire, pause-image seed) plus a `kata-fc` RuntimeClass pinned with `nodeSelector: kubernetes.io/hostname: node-4`. Two facts make almost all of it dead weight:

1. Nothing sets `runtimeClassName: kata-fc`. The RuntimeClass and the devmapper / containerd apparatus that serves it (four of the five scripts) exist for a path no workload uses.
2. The fc-invoke daemon drives Firecracker directly and, since the firecracker binary + guest kernel were baked into its image, needs nothing from the host but `/dev/kvm` and a scratch dir. The last host coupling is gone.

What remains is that fc-invoke is pinned to a specific hostname. Enrolling or replacing the FC node means editing the chart, and the pin is meaningless on a fleet where the node is an EC2 metal instance, not "node-4".

## Decision

1. **Select FC nodes by label, not hostname.** fc-invoke's `nodeSelector` becomes `homelab.io/firecracker: "true"`. Enrolling a node is `kubectl label node <n> homelab.io/firecracker=true`; replacing one is moving the label. On a metal EKS fleet you label the metal instance. Node labels are node config (not GitOps-managed), which is the right layer for "which physical box runs microVMs."

2. **Keep the single-replica Deployment; do not convert to a DaemonSet yet.** At one FC node a DaemonSet is behaviorally identical but opens a Service-to-many-pods routing gap: fc-invoke's ingress routes each `/invoke` to the _local_ executor, so a request landing on a node that lacks that workload's warm base is wrong. Cross-node placement is exactly the `cluster/placement` work ADR 031 defers to when a second node exists, and golden-template snapshots are per-ISA (ADR 033), so a second node is not a free horizontal add anyway. The DaemonSet is the correct shape _with_ placement, not before it.

   **Invariant while this holds:** exactly one node carries `homelab.io/firecracker=true`. A `replicas: 1` Deployment with a label selector must not be free to reschedule between multiple labeled nodes, because the daemon owns node-local scratch and warm-base snapshots that do not follow it. Labeling a second node is the trigger to do the ADR-031 placement + DaemonSet work, not a supported state on its own.

3. **Retire the unused kata-fc path.** Delete the `kata-fc` RuntimeClass chart (`projects/platform/kata-fc/`, and its line in the platform kustomization; ArgoCD prunes the RuntimeClass) and the manual host scripts (`projects/platform/firecracker-node/`). Node-4 still has the devmapper pool and `/opt/kata` installed from the old scripts; they are inert (nothing uses them) and can be cleaned off the host later as hygiene, not as a dependency of this change.

## Consequences

**Positive.** A new or replacement FC node (node-5, or an EKS `*.metal`) goes from a five-script runbook to `kubectl label`. The node contract is now just `/dev/kvm` + scratch, which is the portable minimum. The dead kata-fc apparatus stops being mistaken for the live path.

**Costs / risks.** The one-labeled-node invariant is a convention, not an enforced constraint; labeling a second node before placement lands would let the single replica flap and lose node-local state. This is documented in the values file and is the explicit signal to pick up ADR 031's placement work. Retiring the RuntimeClass is a one-way prune, justified because no workload references it.

## Alternatives considered

- **Convert to a DaemonSet now.** Rejected: it implies the multi-node routing/placement it cannot yet satisfy, and pre-positions a loaded gun (a second labeled node misroutes) for zero benefit at one node. It becomes correct once ADR 031's `cluster/placement` exists.
- **Keep the hostname pin.** Rejected: it is the thing that makes node enrollment a code edit and is meaningless off this cluster.
- **Keep kata-fc "just in case."** Rejected: an unused RuntimeClass plus its devmapper/containerd scripts reads as the live setup path and misleads the next node bring-up. It lives in git history if a pod-shaped kata-fc workload is ever wanted.

## Future work

- ADR 031's `cluster/placement` + the DaemonSet, when a second FC node is warranted (and per ADR 033, per-ISA golden templates for each instance family).
- Optional host hygiene: remove the inert devmapper pool + `/opt/kata` from node-4.
