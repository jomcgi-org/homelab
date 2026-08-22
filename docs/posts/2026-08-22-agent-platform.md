---
title: Agent Platform
date: 2026-08-22
summary: Autonomous agents that do real platform work, each running in its own hardware-isolated Firecracker microVM, snapshotted to near-zero cost when idle and restored from memory in tens of milliseconds on the next turn.
public: false
---

The first version ran unattended Claude agents in sandboxed Kubernetes pods. When Claude's terms of service changed around unattended agent use, that dispatch model no longer held. I split the execution substrate apart from the model choice and rebuilt the substrate on Firecracker: every agent request gets its own hardware-isolated microVM, paused and snapshotted when idle so it costs nothing, and restored in tens of milliseconds when woken. The durable pieces from v1 carried over: on-cluster inference, the MCP gateway, the knowledge-graph surface.

## How it works

**MicroVM per request.** Every agent request gets its own microVM with its own kernel, so code an agent writes runs behind a hardware boundary.

**Snapshot / restore.** An idle agent thread is paused and snapshotted (memory plus rootfs), releasing all compute, then restored on the next turn. Measured restore is 28ms cold, 6ms warm; trigger to first model call is ~140ms. A thin Postgres-reconcile controller owns the lifecycle, porting E2B's open-source snapshot architecture onto the Firecracker primitive.

**Secret-swap egress.** The guest is vsock-only and never holds a real credential. A TLS-terminating sidecar routes by SNI/Host and swaps a placeholder token for the real secret (a GitHub or model API key) at the network hop, so sandboxed code uses credentials it can never read or exfiltrate.

**Postgres control plane.** High-churn idle-agent state lives in Postgres, keeping thousands of waiting threads off the cluster control plane. The same registry backs the list and resume catalog exposed over MCP.

**Local inference.** llama.cpp serves Qwen3.8-27B (dense, 4-bit GGUF) on a single RTX 4090 for routine work; frontier models are reached over the swapped egress only where the task warrants it.

## Source

- [projects/firecracker](https://github.com/jomcgi/homelab/tree/main/projects/firecracker)

<!-- Numbers above were current on 2026-08-22 when this was transcribed from the engineering page. This is a point-in-time post; do not update it, write a new one. -->
