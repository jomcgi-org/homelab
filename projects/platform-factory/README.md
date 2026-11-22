# Factory platform

The [grimoire-data](grimoire-data/) application provisions Grimoire's private
database, object storage keys, runtime identities and their grants. Its
[runbook](../grimoire/deploy/README.md) restores a dump or a backup.

The [t3](t3/) application relays authenticated admin browser traffic to the
T3 socket on node-1. See [T3 browser access](../factory-cluster/README.md#t3-browser-access).

The [pod host access policy](pod-host-access/) warns and audits when application
pods request host networking, host PID, hostPath volumes or privileged containers.

The [image audit](image-audit/) checks rendered platform workloads for digest-pinned images.
