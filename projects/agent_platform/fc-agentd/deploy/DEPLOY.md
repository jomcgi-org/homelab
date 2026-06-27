# fc-agentd deployment runbook (ADR 022)

The controller chart + ArgoCD Application are in this branch, validated with
`helm lint` and `helm template` in both modes. Rollout is two deliberate stages.

## Stage 1 - idle-safe control-plane rollout (default, this PR)

`deploy/values.yaml` ships `firecracker.enabled: false`. The daemon deploys
**non-privileged, non-root (uid 65532), read-only-rootfs, no host mounts**,
connects to the monolith Postgres (`monolith-pg-app` DSN in the `monolith`
namespace), and runs the reconcile loop. With an empty `claude_agent.agent_threads`
table it lists zero threads for node-4 and idles - it never execs Firecracker.

Ship it: merge this PR. CI builds + pushes the `fc-agentd` OCI chart and image;
ArgoCD syncs the `fc-agentd` Application into the `monolith` namespace.

Validate via MCP:

- `monolith-k8s-list-resources kind=pods namespace=monolith` - a `fc-agentd-*`
  pod, Running, pinned to node-4.
- `monolith-k8s-get-pod-logs` (container `fc-agentd`) - `reconcile loop starting
  node=node-4` and a quiet, error-free idle.

## Stage 2 - enable real microVM snapshot/restore (privileged)

This is the hardware-driving step; review the privileged surface first.

1. In `deploy/values.yaml` set `firecracker.enabled: true` and
   `firecracker.rootfsPath` to the thread rootfs block device. This switches the
   pod to `privileged: true`, `runAsUser: 0`, and host mounts `/dev/kvm`,
   `/opt/kata`, `/dev/mapper`, and the snapshot root (`/disks/nvme-02/agent-threads`).
2. Merge; ArgoCD rolls the pod with FC access.
3. Build a warm base for a repo: `monolith-agent-request-base-rebuild repo=<r>
   arch=amd64 main_sha=<sha>`; the controller builds it and records `built_sha`.
4. Submit a task: `monolith-agent-submit-agent-task task=... repo=<r>`. The
   reconcile loop claims a microVM (from the warm base), runs it, and on a
   quiescent idle the wrapper signals a snapshot -> IDLE.
5. Wake it: `monolith-agent-resume-agent-thread thread_id=<id>` (or a Discord
   reply via `wake-agent-thread-for-discord`). Confirm the restored thread
   continues (the heartbeat-continuity check from the ADR derisk).
6. Validate via `monolith-agent-list-agent-threads` and the SigNoz traces
   emitted by `fc-agentd`.

## Downstream MCP tool discovery

The Phase 3-5 agent MCP tools are live in the monolith but Context Forge caches
its catalog. After the monolith rollout, run `/refresh-context-forge-tools`
(needs kubectl) once so `list/get/resume-agent-thread`, `submit-agent-task`,
etc. are exposed to the `homelab` connector.
