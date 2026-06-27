# fc-agentd B5: vsock task delivery, idle signal, egress proxy, snapshot/restore

**Status:** Planned
**ADR:** 022 (Firecracker snapshot/restore controller), Plan B
**Depends on:** B6 (firecracker.enabled, merged + e2e-proven 2026-06-27)

## Context

The substrate is proven end to end: `dispatch.submit` -> `PENDING` row -> `fc-agentd`
claims it -> `CopyProvisioner` copies the base rootfs -> Firecracker boots the
microVM -> `fc-agent-init` (Go PID1) runs and idles -> `RUNNING`; `COMPLETED` ->
Release + reclaim. What is missing is everything that makes the microVM do real
work and idle cheaply:

1. The guest never receives a task (raw FC boot gives PID1 no env).
2. The guest cannot reach in-cluster Qwen (no NIC).
3. The idle-boundary signal does not reach the controller, so nothing snapshots.
4. Restore-on-wake is wired in the reconcile loop but the vsock channel is not
   re-established after a restore.

## Decision: vsock for everything

A single Firecracker vsock device per microVM carries all guest<->controller
communication. No guest NIC, no tap/NAT. The microVM is fully network-isolated;
its only egress is what the controller explicitly proxies (capability-style).

Firecracker vsock is a per-VM host Unix-domain socket that multiplexes by port:

- host -> guest: host connects to `<uds>` and writes `CONNECT <port>\n`.
- guest -> host: host listens on `<uds>_<port>`; guest connects to CID 2 : port.

Per-thread isolation comes from the per-thread UDS path
(`/disks/nvme-02/agent-threads/<id>/vsock.sock`), so the guest needs no identity
of its own: the controller knows which thread a connection belongs to by which
socket it arrived on. All ports are fixed constants; nothing is injected into the
guest.

Ports (constants in a shared package):

- `CONTROL` (e.g. 1024): guest dials host on boot; control frames both ways
  (task push, idle signal, lifecycle).
- `EGRESS` (e.g. 1025): guest dials host per HTTP request; controller proxies to
  the configured upstream (Qwen) and streams the response back.

## Protocol (framed, length-prefixed JSON over the control channel)

- Guest, on boot: dial CID 2 : CONTROL, send `Hello{}`.
- Controller: replies `Assign{recipe, task}` (looked up from the thread row /
  dispatch payload). The guest runs `goose run --recipe <recipe> --params
task_description=<task>` with its OpenAI base URL pointed at the local egress
  shim.
- Guest, when the harness goes quiescent (idle.Detector fires): sends
  `Idle{reason}`. Controller snapshots + pauses (snapshot-on-idle).
- Guest, on harness exit (goose-result terminal marker / process exit): sends
  `Done{status}`. Controller marks the thread COMPLETED.

## Egress shim

`fc-agent-init` runs a tiny in-guest HTTP listener on `127.0.0.1:<port>`. Goose
is configured (config.yaml / env) to use that as its OpenAI-compatible base URL.
For each request the shim opens a vsock connection to CID 2 : EGRESS, writes the
raw HTTP request, and streams the response back. The controller's EGRESS handler
forwards to the real Qwen service URL (injected via Helm values, never a
hardcoded `.svc` default) and streams the response back over vsock.

## Tasks

### Task 1 - shared vsock framing package

`projects/agent_platform/substrate/vsock/` (or `fc-agentd/internal/vsock` shared
with the guest): length-prefixed JSON frame codec + the message types
(`Hello`, `Assign`, `Idle`, `Done`) + the port constants. Pure, unit-tested,
imported by both the controller (host side) and `fc-agent-init` (guest side).

### Task 2 - driver: configure the FC vsock device

`fc-agentd/internal/driver`: on `Claim` (and `Restore`), `PUT /vsock`
`{guest_cid, uds_path: <bundle>/vsock.sock}` before `InstanceStart`. Expose the
uds path on the returned handle so the reconcile loop can reach the control
channel.

### Task 3 - controller: per-thread control server (task push + idle/done)

`fc-agentd`: for each live microVM, accept the guest's CONTROL connection
(listen on `<uds>_<CONTROL>`), send `Assign` from the thread's dispatch payload,
and handle inbound `Idle` / `Done`. `Idle` -> snapshot-on-idle (Task 5); `Done`
-> mark COMPLETED. The dispatch payload (recipe + task) is added to
`claude_agent.agent_threads` (new columns) and written by `dispatch.submit`.

### Task 4 - guest: fc-agent-init dials control + runs the harness

`fc-agent-init`: replace the `FC_CONTROLLER_VSOCK` env gate with an
unconditional dial to CID 2 : CONTROL. Receive `Assign`, build the goose command
(reuse `internal/harness.GooseCommand`), run it with the egress base URL, watch
for the terminal `goose-result` marker + idle boundary, send `Idle` / `Done`.

### Task 5 - snapshot-on-idle

`fc-agentd`: on `Idle`, pause the VM, create an FC snapshot into the bundle,
record `thread_snapshot_ref` + `size_bytes`, set state `IDLE`, release the live
process (the snapshot is the durable form). Reuses the snapshot primitive already
derisked (28 ms cold restore).

### Task 6 - egress proxy (guest shim + controller forwarder)

Guest: `fc-agent-init` runs the `127.0.0.1` HTTP listener that tunnels to CID 2 :
EGRESS. Controller: the EGRESS handler forwards to the Qwen URL (Helm value
`firecracker.egressUpstream`, injected, no hardcoded default). Goose config
points at the shim. This is what lets Goose actually call the model.

### Task 7 - restore-on-wake re-establishes vsock + resumes

`fc-agentd`: `restoreWakeRequested` already restores the snapshot; after restore,
re-accept the control channel and resume the harness (or the snapshot captured a
mid-flight harness that resumes on its own). Validate continuity.

### Task 8 - e2e validation in cluster

`dispatch.submit("<a real small task>")` -> watch the microVM boot, dial control,
receive the task, call Qwen through the egress proxy, produce a result, go idle,
snapshot, then a wake request -> restore -> resume. Validate via fc-agentd logs +
the registry state machine + node-4 (FC process, snapshot files). No local test
loop; validate on the deployed branch.

## Risks / unknowns

- FC vsock host<->guest mechanics on the 6.18.35 kata kernel are unverified
  (PF_VSOCK is registered in the guest; the UDS multiplexing is the unknown).
  Task 2+4 land the minimal dial and Task 8a validates it in-cluster before the
  egress/snapshot layers are built on top.
- Goose's willingness to use a plain-HTTP `127.0.0.1` OpenAI base URL + whether
  it needs TLS. If it insists on TLS, the shim terminates TLS locally with a
  throwaway cert.
- Snapshot-on-idle while the harness holds an open egress connection: snapshot at
  a quiescent boundary only (idle detector guarantees no in-flight request).

## Validation gates

Each task self-reviewed before commit; one comprehensive review at end-of-PR
(per repo convention). All test execution deferred to end-of-plan CI on the
pushed branch, plus the in-cluster e2e in Task 8.
