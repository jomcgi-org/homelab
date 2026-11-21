# Factory platform

The [grimoire-data](grimoire-data/) application provisions Grimoire's private
database, object storage keys, runtime identities and their grants. Its
[runbook](../grimoire/deploy/README.md) restores a dump or a backup.

The [pod host access policy](pod-host-access/) warns and audits when application
pods request host networking, host PID, hostPath volumes or privileged containers.
