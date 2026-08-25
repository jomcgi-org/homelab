# STPA Control Analysis: embervm

_logic @ b1514fe38 · security @ ccaba452d_

_Auto-generated STPA safety model: unsafe states this system can reach and control actions that get it there. Single or multiple lenses: logic (mission failure), security (deliberate attack), governance (data safety)._

<details>
<summary><b>How to read this</b>: STPA primer and diagram legend</summary>

**STPA** (System-Theoretic Process Analysis, Leveson) treats the system as controllers issuing control actions to controlled processes, with feedback flowing back. Instead of "what component fails," it asks "what control action, given or withheld at the wrong time, drives the system into an unsafe state?" Unsafe means a violation of this system's reason to exist. Multiple lenses (logic, security, governance) analyze the same control structure for different safety questions: mission failure, deliberate attack, data-processing violations. A finding appearing in multiple lenses is expected and correct.

Read top-down: Losses are outcomes we must prevent; Hazards are states leading to loss; diagrams show control structure (solid = control action, dashed = feedback); Unsafe Control Actions table is the core; Unsafe Feedback covers data channels (absent, stale, corrupted, spoofed). Every claim cites path:line; unbuilt elements are marked. Semantic stable IDs mean regenerating changes only findings that changed.
</details>

## Control structure

### Logical view

```mermaid
flowchart TD
  subgraph control_plane["control_plane"]
    dispatcher["Admission, quota gate, placement, dispatch"]
    node-registry["Dial-home registry + reconcile inventory"]
    s3-warmth-gc["Artifact TTL sweep (abort on partial listing)"]
    stateful-manager["Stateful wake/bank/blessing/quarantine lifecycle"]
  end
  subgraph ingress["ingress"]
    api["HTTP API + TokenReview auth"]
  end
  subgraph store["store"]
    op-log["Durable ordered journal (book of record)"]
  end
  api -- "api.admit_task" --> dispatcher
  dispatcher -- "dispatcher.quota_gate" --> op-log
  stateful-manager -- "stateful-manager.bless_generation" --> op-log
  stateful-manager -- "stateful-manager.destroy_orphan" --> node-registry
  node-registry -. "reported primed pool inventory (adopt_inventory)" .-> dispatcher
  node-registry -. "reported stateful VM inventory (dial-home NodeStatus), authoritative per invariant 5" .-> stateful-manager
```

### Physical view

```mermaid
flowchart TD
  subgraph compute["compute"]
    egress-proxy["Brick-local egress credential proxy"]
    guest-vm["Firecracker guest VM"]
    node-envoy["node Envoy serving relay (DNAT)"]
    noded["noded brick daemon (privileged, /dev/kvm)"]
    shotter-chromium["Headless Chromium (content-driven fetches)"]
    shotter-guest-init["Shotter guest-init (PID 1): launches Chromium, hosts the proxy, serves /shim/ready"]
    shotter-proxy["In-guest egress allowlist proxy (image-baked policy)"]
  end
  subgraph control_plane["control_plane"]
    control-plane["Elixir control plane pod (1 replica, Recreate)"]
  end
  subgraph data["data"]
    s3-store["S3-compatible object store (SeaweedFS gateway)"]
  end
  subgraph external["external"]
    caller["External task/session/serving caller"]
    external-host["External destination (e.g. GitHub API)"]
  end
  control-plane -- "control-plane.grpc_command" --> noded
  egress-proxy -- "egress-proxy.inject_credential" --> external-host
  node-envoy -- "node-envoy.dnat_route" --> guest-vm
  noded -- "noded.artifact_verb" --> s3-store
  noded -- "noded.refuse_if_silenced" --> noded
  noded -- "noded.vsock_dispatch" --> guest-vm
  shotter-chromium -- "shotter-chromium.fetch_subresource" --> shotter-proxy
  shotter-proxy -- "shotter-proxy.forward_allowed" --> egress-proxy
  control-plane -. "contact signal behind the silence gate: a 2xx dial-home Register response and every successful WatchNode NodeStatus send refresh lastContact, which refuseIfSilenced compares against the 21600s bound" .-> noded
  noded -. "dial-home registration {node, pod_uid, address, boot_id} + NodeStatus" .-> control-plane
  s3-store -. "fetched artifact bytes on RestoreArtifact" .-> noded
  shotter-guest-init -. "GET /shim/ready 200 (CDP /json/version answered and one real trial capture produced non-empty PNG bytes)" .-> noded
```

## Control actions

| ID | View | Control action | Controller → Process | Maturity | Evidence |
|----|----|----|----|----|----|
| `api.admit_task` | logical | Admit task workload | `api` → `dispatcher` | built | projects/embervm/control/lib/embervm/router.ex:33 |
| `control-plane.grpc_command` | physical | gRPC lifecycle command (BuildBase, Prime, Assign, Destroy, DeleteVolume, RestoreArtifact) | `control-plane` → `noded` | built | projects/embervm/proto/embervm/node/v1/node.proto:57 |
| `dispatcher.quota_gate` | logical | Gate quota on admission | `dispatcher` → `op-log` | built | projects/embervm/control/lib/embervm/dispatcher.ex:258 |
| `egress-proxy.inject_credential` | physical | Inject credential into request | `egress-proxy` → `external-host` | built | projects/embervm/ARCHITECTURE.md:710 |
| `node-envoy.dnat_route` | physical | DNAT route to guest | `node-envoy` → `guest-vm` | built | projects/embervm/ARCHITECTURE.md:193 |
| `noded.artifact_verb` | physical | Access artifact in store (get, put, delete, list, tag) | `noded` → `s3-store` | built | projects/embervm/proto/embervm/node/v1/node.proto:463 |
| `noded.refuse_if_silenced` | physical | Refuse local authority if control-plane contact stale | `noded` → `noded` | built | projects/embervm/noded/server/server.go:619 |
| `noded.vsock_dispatch` | physical | vsock dispatch to guest | `noded` → `guest-vm` | built | projects/embervm/ARCHITECTURE.md:234 |
| `shotter-chromium.fetch_subresource` | physical | Fetch subresource from rendered page | `shotter-chromium` → `shotter-proxy` | built | projects/embervm/runtimes/shotter/guest-init/cmd/main.go:168 |
| `shotter-proxy.forward_allowed` | physical | Forward request to allowlisted destination | `shotter-proxy` → `egress-proxy` | built | projects/embervm/runtimes/shotter/guest-init/cmd/proxy.go:391-405 |
| `stateful-manager.bless_generation` | logical | Issue blessed generation | `stateful-manager` → `op-log` | built | projects/embervm/control/lib/embervm/stateful_manager.ex:676 |
| `stateful-manager.destroy_orphan` | logical | Destroy orphaned VM | `stateful-manager` → `node-registry` | built | projects/embervm/control/lib/embervm/stateful_manager.ex:2027 |

## Lens: logic

**Scope.** EmberVM is a Firecracker microVM platform: an Elixir control plane admits and dispatches task, session, serving, and stateful workloads onto brick pods, each run by a privileged Go noded daemon that supervises guest VMs over vsock and dial-homes to register with the control plane.

<details>
<summary>Maturity detail</summary>

- **Built:** task/session/serving/stateful/composite lifecycle managers, admission quota gate (fail-closed per-principal when a budget is configured), generation blessing (CP-issued pre-dispatch and checkpoint-abort self-heal via durable checkpoint_dispatched), delegated generation leases (BlessingLease, bounded and monotonic), orphan-destroy reconcile with an ACTIVATOR-origin adoption guard that runs before the destroy pass, S3 warmth GC that aborts the whole sweep on a partial listing, dial-home registration, brick-local egress credential injection scoped by egressTo, the shotter task-class guest (ADR embervm/035): a warm headless Chromium snapshot base behind an in-guest, image-baked, hard-allowlisted egress proxy, whose readiness is now gated on one real trial capture before /shim/ready flips, the noded gRPC control channel, authenticated by a static bearer token and scoped by a CiliumNetworkPolicy to control-plane-only ingress, enabled in production (#4693), SigV4-authenticated access to the S3 artifact store enforced by the gateway (enableAuth: true), enabled in production (#4708); the surviving embervm identity is one credential shared by every brick and the control plane, the brick silence timeout (ADR embervm/037) bounding node-local authority (activator wakes, group wakes, blessing-lease self-advance) once control-plane contact goes stale, armed at 21600s in production and dev, the warmth ownership transition guard (#4962): every brick refreshes a .alive claim per warmth segment and startup GC reaps only unclaimed pre-heartbeat directories or claims older than WarmthStaleAfter, principal-artifact envelope encryption and capability-gated restore in noded, reader path unconditional, writer and enforcement armed in production and dev as of 2026-08 (ADR embervm/033, #4691; status updated 2026-08-23 (#5233))
- **Designed-only:** mTLS/SPIFFE certificate-based mutual transport auth for noded (#4693 delivered the bearer-token-plus-NetworkPolicy interim), node-local activator for stateful/composite wake beyond cold boot (partially landed, stateful activator currently only cold-boots), audience-scoped guest token, request-scoped GitHub tool mediation replacing host-keyed injection (ADR 055)
</details>

### Losses

| ID | Loss |
|----|------|
| `L.integrity-loss` | Stored or served state (volume, snapshot, generation, artifact) diverges from truth and is acted on as correct |
| `L.liveness-loss` | The fleet, a brick, or a workload becomes unable to admit or serve legitimate work |
| `L.provenance-loss` | State, credentials, or execution history become attributed to the wrong principal, brick, or generation |
| `L.secret-exposure` | Credential or key material becomes reachable by an unauthorized principal or guest |
| `L.silent-incorrectness` | A caller receives a served or resumed result derived from stale, forged, or wrong state with no signal |
| `L.unauthorized-access` | An actor obtains a capability, credential reach, or execution path beyond its principal or role |

### Hazards

| ID | View | Hazard (unsafe state) | → Losses | Maturity |
|----|----|----|----|----|
| `egress-workload-derivation-gap` | physical | the per-workload egress forwarder allowlist noded receives is a hand-written if-chain over named workloads in the chart template, not a true derivation, so a future task/session/stateful workload that enables egress but is not added to this chain gets no forwarder and dial-times-out inside the guest instead of a loud, diagnosable deny | L.liveness-loss | built |
| `host-keyed-credential-overreach` | physical | host-keyed egress credential injection authorizes by destination host only, so a prompt-injected or compromised guest can shape any request to an allowlisted host and have the credential attached to it | L.secret-exposure, L.unauthorized-access | built |
| `identity-hijack` | physical | an actor holding the shared noded ServiceAccount token can re-register an existing brick's (node, pod_uid) at an address it controls and become the dial-home source the control plane treats as authoritative for that brick | L.unauthorized-access, L.integrity-loss, L.provenance-loss | built |
| `shared-egress-allowlist-widening` | physical | egress.internal.allowlist is global to the shared sidecar rather than scoped per workload, so shotter's two new frontend destinations become reachable by every other egress-enabled workload (today the claude runtime, later pi if granted egress) with no per-workload authorization check | L.unauthorized-access | built |
| `shotter-broken-base-snapshot` | physical | shotter's base snapshot is cut when /shim/ready first returns 200; readiness now proves more than protocol liveness because guest-init must answer CDP /json/version and then complete one real trial capture (navigate about:blank, screenshot, non-empty PNG bytes within 15s) before flipping ready, so a globally broken renderer or GL stack fails loudly at warm-up instead of baking into every clone, but the trial exercises only about:blank, so a page-specific rendering failure can still be baked into the base and fail per-invocation instead of failing once, loudly, at BuildBase | L.liveness-loss, L.silent-incorrectness | built |
| `shotter-memmib-tmpfs-coupling` | physical | the shotter workload's memMib budget and its guest-init tmpfs size are a YAML integer and a Go string literal in different directories with nothing in the build enforcing their relation, so raising either alone silently trades a legible ENOSPC on /tmp for an opaque guest OOM kill under load, or leaves /tmp undersized without freeing any more memMib | L.liveness-loss, L.silent-incorrectness | built |
| `shotter-policy-drift` | physical | the guest's baked destination policy (/etc/shotter-egress.json, a checked-in file copied verbatim into the image) and the sidecar's chart-driven egress.internal.allowlist are two independently hand-maintained sources of egress truth with no build-time check that they agree, so they can diverge in either direction: the guest could deny a destination the chart already permits (silent functional failure), or a future image rebuild could widen the baked allowlist beyond what any ADR or chart review anticipated | L.unauthorized-access, L.silent-incorrectness | built |
| `store-credential-unscoped` | physical | the S3 gateway now requires a valid SigV4-signed identity, but the embervm identity's credential is one secret rendered into every noded pod and the control plane, with bucket-wide read/write/list/tag reach over both the embervm and embervm-dev buckets; restore authorization is still storage-ACL-only, not scoped by (principal, lineage, brick, workload, generation, lease), so any brick holding the shared credential can write, substitute, or delete another principal's artifacts, bypassing noded's per-request artifact-verb checks entirely | L.integrity-loss, L.silent-incorrectness | built |
| `unmodeled-checkpoint-abort` | logical | the interruptible-bank checkpoint commit/abort protocol (generation-advance-then-delete-temp-then-resume ordering, resolve-timeout auto-abort, blessed-vs-self-bump discrimination) governs whether a resumed VM's generation pairing stays trustworthy, but is verified only by code comments, unlike the bank/relight pairing invariant it sits underneath | L.integrity-loss, L.silent-incorrectness | built |

### Unsafe control actions

| ID | View | Control action | Guideword | Unsafe condition | Severity | → Hazards | Evidence |
|----|----|----|----|----|----|----|----|
| `egress-proxy.inject_credential.providing` | physical | `egress-proxy.inject_credential` | providing | the proxy injects a real credential into any request whose destination host is allowlisted, regardless of what the guest-originated request actually asks that host to do, so a prompt-injected guest can direct the credentialed call | medium | host-keyed-credential-overreach | projects/embervm/ARCHITECTURE.md:745 |

### Unsafe feedback

*Data channels (absent, stale, corrupted, spoofed) whose failure drives a controller into a hazard.*

| ID | View | Channel | Guideword | Unsafe condition | Severity | → Hazards | Evidence |
|----|----|----|----|----|----|----|----|
| `dial-home.unauthorized-source` | physical | `noded` → `control-plane`: dial-home registration {node, pod_uid, address, boot_id} + NodeStatus | unauthorized-source | re-registering an existing (node, pod_uid) at a different address is accepted unconditionally and expires the prior instance, so identity is self-asserted under a ServiceAccount shared by every brick rather than bound to the actual brick | high | identity-hijack | projects/embervm/control/lib/embervm/node_registry.ex:1320 |
| `shotter-readiness.corrupted` | physical | `shotter-guest-init` → `noded`: GET /shim/ready 200 (CDP /json/version answered and one real trial capture produced non-empty PNG bytes) | corrupted | readiness no longer gates on protocol liveness alone: guest-init runs one real trial capture (navigate about:blank over CDP, screenshot, non-empty PNG bytes required within 15s) before /shim/ready flips, so a globally non-rendering browser now fails BuildBase loudly; the residual staleness is that the trial page is static about:blank, so readiness still cannot prove rendering of real fetched pages, and a page-specific break baked into the base surfaces per invocation instead of once at BuildBase | low | shotter-broken-base-snapshot | projects/embervm/runtimes/shotter/guest-init/cmd/main.go:127-139,183-206 |
| `warmth-fetch.unauthorized-source` | physical | `s3-store` → `noded`: fetched artifact bytes on RestoreArtifact | unauthorized-source | the gateway now enforces SigV4 (enableAuth: true) and rejects requests signed by no identity, but the embervm identity's access key is one credential shared by every brick and the control plane rather than scoped per principal, lineage, brick, workload, or generation, so an object noded restores as warmth may have been written by any brick holding that shared credential, not necessarily the legitimate owner of the lineage being restored; the finer per-principal binding (envelope encryption plus capability-gated restore) is built and enforced in production and dev (#5233 status update) | medium | store-credential-unscoped | projects/embervm/chart/templates/_noded-pod.tpl:353 |

<details>
<summary><b>Not UCAs</b>: 13 examined and rejected</summary>

- **a fleet-wide freeze of new session/serving/stateful placement during a genuine control-plane outage longer than the 6h brick silence bound (ADR embervm/037)**: a deliberate, accepted trade-off (ADR embervm/037 consequences): the gate only refuses NEW work, never banks or destroys anything, so live VMs keep running and held warmth stays intact; normal service resumes immediately once contact returns (noded/server/server.go:608-625), and 6h was sized specifically to exceed a routine control-plane roll
- **a leaked Chromium CDP target accumulating across shotter invocations**: each invocation restores its own CoW clone that is destroyed after the response, so a leak cannot outlive one clone; within a clone, closeCDPTarget runs on every return path via defer immediately after target creation, including navigation and capture errors (projects/embervm/chart/templates/workload-shotter.yaml, guest-init/cmd/cdp.go:467-481)
- **a redirect or subresource on a mapped page escaping the shotter-proxy allowlist**: the proxy is the only egress path (--proxy-server with loopback-only bypass, no direct-network fallback), so a redirect or a subresource fetch to an unmapped host issues a fresh CONNECT/absolute request through the same proxy, is re-parsed and refused per connection by handleConnection, and is checked by the same resolve() call as any other request, never trusted because it originated from an already-allowed page (projects/embervm/runtimes/shotter/guest-init/cmd/main.go:168-175, guest-init/cmd/proxy.go:293-313,391-405)
- **a restored clone resuming a page or CDP target left over from a prior invocation**: createCDPTarget opens a fresh about:blank target per invocation specifically so an hours-later restore never inherits a stale page or target (projects/embervm/runtimes/shotter/guest-init/cmd/cdp.go:46-47)
- **checkpoint-abort auto-bump producing an unblessed generation**: the resolve-timeout auto-abort lane (blessedGeneration: 0) is the ONLY case that produces it, and StatefulStore correctly quarantines it as a fail-closed signal rather than treating it as a bug (noded/server/stateful.go:605-610)
- **no per-principal daily budget configured in the reference deployment**: spend is still bounded by admission caps and concurrency, not unbounded; cutoff is an admission action by design (invariant 4), and quota fails closed the moment a budget IS set (control/lib/embervm/dispatcher.ex:265-269, deploy/values.yaml)
- **node-local activator on stateful defaults BlessedGeneration to 0 during control-plane absence**: a zero generation fails pairing and forces a cold boot rather than an incorrect relight, matching invariant 4's fail-open-to-cold-boot rule (noded/server/stateful_activator.go:361)
- **orphan-destroy racing a live node-woken (ACTIVATOR-origin) stateful VM**: adopt_activator_stateful_vms runs on the same reconcile pass before the orphan-destroy loop, and the loop explicitly skips activator_origin? vms as belt-and-suspenders (control/lib/embervm/stateful_manager.ex:2031-2038)
- **shotter-proxy refusing every destination when /etc/shotter-egress.json is missing or malformed**: LoadProxyConfig returns a zero-value config on any read or parse error, and the zero value's nil maps make resolve() refuse everything; the failure mode is fail-closed and surfaces as a bounded per-request refusal, not a silent bypass (projects/embervm/runtimes/shotter/guest-init/cmd/proxy.go:78-99)
- **shotter-proxy.forward_allowed checked against the requested top-level host instead of the actually-dialled destination**: resolve() applies the host mapping first and validates the resulting mapped destination against the allowlist for both CONNECT targets and Host-header-derived absolute-form requests, so the value checked and the value dialled are always the same one; a request for an unmapped or mismatched host fails resolve() before any vsock dial (projects/embervm/runtimes/shotter/guest-init/cmd/proxy.go:211-247)
- **the S3 artifact-store gateway accepting anonymous, unsigned requests (formerly hazard anonymous-store-access)**: closed in production: SeaweedFS S3 auth is enabled (enableAuth: true, projects/platform/seaweedfs/values.yaml:181) with the embervm identity policy (s3-identities.json), so an unsigned request is now rejected at the gateway; the residual gap, that the surviving identity is shared fleet-wide rather than scoped per principal, is tracked separately as hazard store-credential-unscoped (#4708, closed)
- **the brick silence gate firing while the control plane is actually alive, wrongly refusing activator wakes, group wakes, or blessing-lease self-advancement (ADR embervm/037 wrong-timing)**: rejected: lastContact advances only on authenticated control-plane contact through two independent channels, a 2xx dial-home POST on the roughly 30s jittered register loop (noded/server/register.go:96-98,198-203) and every successful WatchNode NodeStatus send on the 2s liveness interval (noded/server/server.go:70,1801), compared with time.Now's monotonic reading so an NTP step cannot arm or disarm the gate (noded/server/server.go:595-597); tripping it requires both channels to fail simultaneously for the full 21600s bound (deploy/values.yaml:195), which is itself the brick partition the gate exists to catch, and even a false trip is bounded refusal of NEW work with live VMs, banks, and held warmth untouched until contact returns
- **the noded gRPC control channel accepting BuildBase/Prime/Assign/Destroy/DeleteVolume/RestoreArtifact from any pod-network caller (formerly hazard open-node-control-channel)**: closed in production: the gRPC surface is now bearer-token authenticated (EMBERVM_NODED_BEARER_TOKEN gates unaryAuthInterceptor/streamAuthInterceptor, noded/cmd/main.go:286-293) AND a CiliumNetworkPolicy restricts gRPC-port ingress to only the control-plane pod's own selector labels, excluding noded's own brick labels (chart/templates/noded-networkpolicy.yaml:14-23); both bearerTokenSecret.enabled and networkPolicy.enabled are true in deploy/values.yaml (#4693, closed)
</details>

### Open questions

- Whether a chart conformance test should pin the stateful/composite activator port ranges (5400-5419) into noded-networkpolicy.yaml: the 2026-08-22 first enable omitted them and dropped a cold wake to noded:5401 for 14 minutes (deploy/values.yaml:163-166), and nothing structural prevents a repeat edit from doing the same.
- Whether egress.internal.allowlist should become scoped per egress-enabled workload rather than global to the sidecar (ADR embervm/035 open question 1); shared-egress-allowlist-widening tracks the safety consequence of leaving it global.
- Whether the store-credential-unscoped residual (a shared, bucket-wide S3 identity) should be re-scoped per principal now that the ADR embervm/033 rollout is armed end to end in production and dev (kekRoot, EMBERVM_ARTIFACT_ENCRYPTION, store.encrypt, requireRestoreCapability; #4691, status updated 2026-08-23 (#5233)): what survives of the residual is write/delete reach and key custody, since reads of encrypted warmth yield ciphertext.

## Lens: security

**Scope.** Deliberate attacks on EmberVM's control structure: an adversary who forges, withholds, or replays control actions or feedback. Same control structure as the logic lens, analyzed for attack rather than honest failure.

<details>
<summary>Maturity detail</summary>

- **Built:** bearer-token authentication on every unary and streaming gRPC call plus CiliumNetworkPolicy restricting noded ingress to control-plane pod selector labels (#4693); SigV4-authenticated access to S3 artifact store enforced by gateway (#4708); brick dial-home registration bound to the noded pod's projected token claim identity (#4707, closed by #5049); per-principal envelope encryption of mutable state and capability-gated restore authorization (#4691, armed in production and dev 2026-08); the brick silence timeout (ADR embervm/037) bounding node-local authority once control-plane contact goes stale, armed at 21600s in production and dev; warmth ownership transition guard (#4962) refreshing .alive claims and reaping unclaimed pre-heartbeat directories
- **Designed-only:** mTLS/SPIFFE certificate-based mutual transport auth for noded, request-scoped GitHub tool mediation replacing host-keyed injection (ADR agents/055), digest-verified manifests for restored artifact integrity (ADR embervm/033 decision 3), per-workload egress allowlist scoping (ADR embervm/035)
</details>

### Losses

| ID | Loss |
|----|------|
| `L.capacity-exhaustion` | A malicious tenant or unauthorized actor exhausts shared placement capacity, quota, or concurrency |
| `L.credential-theft` | Egress credential, store credential, or bearer token is obtained and used by an unauthorized actor |
| `L.host-compromise` | A brick pod is compromised via a VMM escape or noded vulnerability and holds privileged access, store credential, and bearer token |
| `L.tenant-data-corruption` | Another tenant's mutable state is written, deleted, or substituted by an unauthorized principal or brick |
| `L.tenant-data-disclosure` | Another tenant's memory snapshot, session workspace, or state is read by an unauthorized principal |

### Hazards

| ID | View | Hazard (unsafe state) | → Losses | Maturity | Status | Issue |
|----|----|----|----|----|----|----|
| `firecracker-no-jailer` | physical | noded execs the firecracker binary directly, as root, inside a privileged pod; the only per-VM containment added is a mount namespace for vsock isolation. Firecracker's built-in seccomp filter is active, but a VMM escape lands in a process holding /dev/kvm, the fleet-shared store credential, and the noded bearer token, becoming the compromised-brick adversary instantly. | L.host-compromise | built | none | #5255 |
| `host-keyed-credential-overreach` | physical | host-keyed egress credential injection authorizes by destination host only, so a prompt-injected or compromised guest can shape any request to an allowlisted host and have the credential attached to it | L.credential-theft | built | designed |  |
| `identity-hijack` | physical | an actor holding the shared noded ServiceAccount token can re-register an existing brick's (node, pod_uid) at an address it controls and become the dial-home source the control plane treats as authoritative for that brick | L.credential-theft, L.host-compromise | built | enforced-prod |  |
| `shared-egress-allowlist-widening` | physical | egress.internal.allowlist is global to the shared sidecar rather than scoped per workload, so shotter's two new frontend destinations become reachable by every other egress-enabled workload (today the claude runtime, later pi if granted egress) with no per-workload authorization check | L.credential-theft | built | none | #5320 |
| `store-credential-unscoped` | physical | the S3 gateway now requires a valid SigV4-signed identity, but the embervm identity's credential is one secret rendered into every noded pod and the control plane, with bucket-wide read/write/list/tag reach over both the embervm and embervm-dev buckets; restore authorization is still storage-ACL-only, not scoped by (principal, lineage, brick, workload, generation, lease), so any brick holding the shared credential can write, substitute, or delete another principal's artifacts, bypassing noded's per-request artifact-verb checks entirely | L.tenant-data-corruption, L.tenant-data-disclosure | built | none | #4691 |

### Unsafe control actions

| ID | View | Control action | Guideword | Unsafe condition | Severity | → Hazards | Status | Issue | Evidence |
|----|----|----|----|----|----|----|----|----|----|
| `api.admit_task.providing` | logical | `api.admit_task` | providing | a malicious principal submits a workload that references another principal's artifact ref or lineage, bypassing the invariant 3 boundary (no mutable VM or snapshot lineage crosses a principal); the principal-scoped keyspace for the shared prefix remains designed but not implemented | high | store-credential-unscoped | enforced-prod | #4691 | projects/embervm/control/lib/embervm/dispatcher.ex:130 |
| `control-plane.grpc_command.providing` | physical | `control-plane.grpc_command` | providing | an attacker on the cluster network calls noded's gRPC surface (BuildBase, Assign, Destroy, RestoreArtifact) directly from anywhere on the pod network without a legitimate identity | high | identity-hijack | enforced-prod | #4693 | projects/embervm/chart/templates/noded-networkpolicy.yaml:14-23 |
| `egress-proxy.inject_credential.providing` | physical | `egress-proxy.inject_credential` | providing | a prompt-injected guest gets a real credential attached to a request it authored, not one the operator intended; the decided direction (request-scoped tool mediation for at least the git credential class) is drafted in ADR agents/055 and not yet the default for every credential | high | host-keyed-credential-overreach | enforced-prod |  | projects/embervm/ARCHITECTURE.md:745 |
| `noded.artifact_verb.providing` | physical | `noded.artifact_verb` | providing | a compromised brick or a credential holder can write, evict, or substitute another principal's artifact; the store credential is still bucket-wide and fleet-shared, so a compromised brick can delete or overwrite any principal's artifacts | high | store-credential-unscoped | enforced-prod | #4691 | projects/embervm/chart/templates/_noded-pod.tpl:353 |

### Unsafe feedback

*Data channels (absent, stale, corrupted, spoofed) whose failure drives a controller into a hazard.*

| ID | View | Channel | Guideword | Unsafe condition | Severity | → Hazards | Status | Issue | Evidence |
|----|----|----|----|----|----|----|----|----|----|
| `dial-home.unauthorized-source` | physical | `noded` → `control-plane`: dial-home registration {node, pod_uid, address, boot_id} + NodeStatus | unauthorized-source | an attacker re-registers an existing (node, pod_uid) at a different address using the shared ServiceAccount every brick already holds, attempting to become the dial-home source the control plane treats as authoritative for that brick; the register route now refuses a body whose pod_uid or node does not match the bound projected token's own claims, a token with no pod binding, or an unset noded SA | high | identity-hijack | enforced-prod |  | projects/embervm/control/lib/embervm/router.ex:909-913 |
| `warmth-fetch.unauthorized-source` | physical | `s3-store` → `noded`: fetched artifact bytes on RestoreArtifact | unauthorized-source | a memory snapshot or artifact noded restores as warmth may have been written by any brick holding that shared credential, not necessarily the legitimate owner of the lineage being restored; the credential itself is still one identity with bucket-wide write, list, and delete reach | high | store-credential-unscoped | enforced-prod | #4691 | projects/embervm/chart/templates/_noded-pod.tpl:353 |

<details>
<summary><b>Not UCAs</b>: 1 examined and rejected</summary>

- **a guest with no cluster credential accessing the store directly**: noded is the sole store client; guests hold no cluster credential by construction, and have no network path to the store (ARCHITECTURE.md section 9)
</details>

### Open questions

- Whether digest-verified manifests for artifact integrity (ADR embervm/033 decision 3) should be implemented to detect deliberate artifact substitution by a credential holder
- Whether per-workload egress allowlist scoping (ADR embervm/035 open question 1) should be implemented to prevent shared-egress-allowlist-widening
- Whether request-scoped GitHub tool mediation (ADR agents/055) should replace host-keyed egress credential injection
- shared egress allowlist provides no per-workload isolation (ADR embervm/035), making destinations added for one workload reachable by all egress-enabled workloads with no per-workload authorization check
