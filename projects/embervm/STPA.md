# STPA Control Analysis: EmberVM @ 605ec320a

_Auto-generated STPA safety model: the unsafe states this system can reach and the control actions that get it there. Two views: logical (functional control flow) and physical (deployment)._

<details>
<summary><b>How to read this</b>: STPA primer and diagram legend</summary>

**STPA** (System-Theoretic Process Analysis) treats the system as *controllers* issuing *control actions* to *controlled processes*, with *feedback* flowing back up. Instead of "what component can fail," it asks "what control action, given or withheld at the wrong time, drives the system into an unsafe state?" "Unsafe" means a violation of this system's reason to exist, not merely a crash.

Read top-down: **Losses** are outcomes we must never cause; **Hazards** are system states that lead to a loss; the **control-structure diagrams** (one per view) show who commands whom (solid arrows = control actions, dashed = feedback, a node tagged `(designed)` is in the architecture but **not yet built**); the **Unsafe Control Actions** table is the core, and **Unsafe Feedback** covers the dashed arrows: data channels whose absence, staleness, corruption, or spoofing drives a controller into a hazard. Every claim cites `path:line`; unbuilt elements are marked. Semantic, stable IDs mean regenerating changes only the findings that changed.
</details>

**Scope.** EmberVM's Elixir control plane and Go noded daemons implement a well-verified lifecycle core (three TLA+ specs, fail-closed admission/destroy/quarantine paths), but three deployment-time trust boundaries default open: unauthenticated noded gRPC, self-asserted dial-home identity under a shared ServiceAccount, and an anonymous default object-store gateway.

<details>
<summary>Maturity detail</summary>

- **Built:** task/session/serving/stateful/composite lifecycle managers; admission quota gate (fail-closed per-principal when a budget is configured); generation blessing (CP-issued pre-dispatch and checkpoint-abort self-heal via durable checkpoint_dispatched); delegated generation leases (BlessingLease, bounded and monotonic); orphan-destroy reconcile with an ACTIVATOR-origin adoption guard that runs before the destroy pass; S3 warmth GC that aborts the whole sweep on a partial listing; dial-home registration; brick-local egress credential injection scoped by egressTo
- **Designed-only:** mTLS/SPIFFE transport auth and NetworkPolicy for noded (#4693); per-principal envelope encryption and tuple-authorized restore (#4691); node-local activator for stateful/composite wake (partially landed, stateful activator currently only cold-boots); audience-scoped guest token; request-scoped GitHub tool mediation replacing host-keyed injection (ADR 055)
- **Note:** The interruptible-bank checkpoint commit/abort sub-protocol (noded/server/stateful.go) is exercised in production and depends on a hand-reasoned crash-ordering argument, but has no TLA+ coverage, unlike the top-level bank/relight pairing invariant it sits underneath (specs/bank_relight.tla has zero mentions of checkpoint/interruptible).
</details>

## Control structure

### Logical view

```mermaid
flowchart TD
  subgraph control_plane["control-plane"]
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
  end
  subgraph control_plane["control-plane"]
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
  noded -- "noded.vsock_dispatch" --> guest-vm
  noded -. "dial-home registration {node, pod_uid, address, boot_id} + NodeStatus" .-> control-plane
  s3-store -. "fetched artifact bytes on RestoreArtifact" .-> noded
```

## Losses

| ID | Loss |
|----|------|
| `L.integrity-loss` | Stored or served state (volume, snapshot, generation, artifact) diverges from truth and is acted on as correct |
| `L.liveness-loss` | The fleet, a brick, or a workload becomes unable to admit or serve legitimate work |
| `L.provenance-loss` | State, credentials, or execution history become attributed to the wrong principal, brick, or generation |
| `L.secret-exposure` | Credential or key material becomes reachable by an unauthorized principal or guest |
| `L.silent-incorrectness` | A caller receives a served or resumed result derived from stale, forged, or wrong state with no signal |
| `L.unauthorized-access` | An actor obtains a capability, credential reach, or execution path beyond its principal or role |

## Hazards

| ID | View | Hazard (unsafe state) | → Losses | Maturity |
|----|----|----|----|----|
| `anonymous-store-access` | physical | the reference deployment's default S3 gateway accepts unauthenticated requests, so any pod-network caller can write, substitute, or delete warmth and archive objects directly, bypassing every noded-enforced artifact-verb safety check; restore authorization today is storage-ACL-only, not scoped by (principal, lineage, brick, workload, generation, lease) | L.integrity-loss, L.secret-exposure, L.silent-incorrectness | built |
| `host-keyed-credential-overreach` | physical | host-keyed egress credential injection authorizes by destination host only, so a prompt-injected or compromised guest can shape any request to an allowlisted host and have the credential attached to it | L.secret-exposure, L.unauthorized-access | built |
| `identity-hijack` | physical | an actor holding the shared noded ServiceAccount token can re-register an existing brick's (node, pod_uid) at an address it controls and become the dial-home source the control plane treats as authoritative for that brick | L.unauthorized-access, L.integrity-loss, L.provenance-loss | built |
| `open-node-control-channel` | physical | noded's gRPC surface accepts BuildBase/Prime/Assign/Destroy/DeleteVolume/RestoreArtifact from any caller reachable on the pod network, because the bearer-token interceptor and the covering NetworkPolicy are both off by default | L.unauthorized-access, L.integrity-loss | built |
| `unmodeled-checkpoint-abort` | logical | the interruptible-bank checkpoint commit/abort protocol (generation-advance-then-delete-temp-then-resume ordering, resolve-timeout auto-abort, blessed-vs-self-bump discrimination) governs whether a resumed VM's generation pairing stays trustworthy, but is verified only by code comments, unlike the bank/relight pairing invariant it sits underneath | L.integrity-loss, L.silent-incorrectness | built |

## Control actions

| ID | View | Control action | Controller → Process | Maturity | Evidence |
|----|----|----|----|----|----|
| `api.admit_task` | logical | Authenticate + admit a task/session submission | `api` → `dispatcher` | built | projects/embervm/control/lib/embervm/router.ex:33 |
| `control-plane.grpc_command` | physical | BuildBase / Prime / Assign / Destroy / DeleteVolume / RestoreArtifact | `control-plane` → `noded` | built | projects/embervm/proto/embervm/node/v1/node.proto:57 |
| `dispatcher.quota_gate` | logical | Fail-closed per-principal budget check before dispatch | `dispatcher` → `op-log` | built | projects/embervm/control/lib/embervm/dispatcher.ex:258 |
| `egress-proxy.inject_credential` | physical | Header-inject a credential, host-keyed by egressTo | `egress-proxy` → `external-host` | built | projects/embervm/ARCHITECTURE.md:642 |
| `node-envoy.dnat_route` | physical | Kernel DNAT of serving traffic into guest tap | `node-envoy` → `guest-vm` | built | projects/embervm/ARCHITECTURE.md:145 |
| `noded.artifact_verb` | physical | ExportArtifact / RestoreArtifact / EvictArtifact | `noded` → `s3-store` | built | projects/embervm/proto/embervm/node/v1/node.proto:463 |
| `noded.vsock_dispatch` | physical | Dispatch payload to guest over vsock (no NIC) | `noded` → `guest-vm` | built | projects/embervm/ARCHITECTURE.md:142 |
| `stateful-manager.bless_generation` | logical | Durably bless the next volume generation before dispatch | `stateful-manager` → `op-log` | built | projects/embervm/control/lib/embervm/stateful_manager.ex:676 |
| `stateful-manager.destroy_orphan` | logical | Fail-closed destroy of an unrecognized node-reported VM | `stateful-manager` → `node-registry` | built | projects/embervm/control/lib/embervm/stateful_manager.ex:2027 |

## Unsafe control actions

*The core of the analysis. Each row: a control action made unsafe via one guideword, the hazard/loss it causes, and where in the code it lives.*

| ID | View | Control action | Guideword | Unsafe condition | Severity | → Hazards | Evidence |
|----|----|----|----|----|----|----|----|
| `control-plane.grpc_command.providing` | physical | `control-plane.grpc_command` | providing | an actor other than the control plane issues BuildBase/Prime/Assign/Destroy/DeleteVolume/RestoreArtifact to a brick, since noded runs with no bearer token and no NetworkPolicy selects it | high | open-node-control-channel | projects/embervm/noded/cmd/main.go:245 |
| `egress-proxy.inject_credential.providing` | physical | `egress-proxy.inject_credential` | providing | the proxy injects a real credential into any request whose destination host is allowlisted, regardless of what the guest-originated request actually asks that host to do, so a prompt-injected guest can direct the credentialed call | medium | host-keyed-credential-overreach | projects/embervm/ARCHITECTURE.md:667 |

## Unsafe feedback

*Feedback and data channels whose absence, staleness, corruption, or spoofed origin drives a controller into a hazard. This is where data-integrity failures live.*

| ID | View | Channel | Guideword | Unsafe condition | Severity | → Hazards | Evidence |
|----|----|----|----|----|----|----|----|
| `dial-home.unauthorized-source` | physical | `noded` → `control-plane`: dial-home registration {node, pod_uid, address, boot_id} | unauthorized-source | re-registering an existing (node, pod_uid) at a different address is accepted unconditionally and expires the prior instance, so identity is self-asserted under a ServiceAccount shared by every brick rather than bound to the actual brick | high | identity-hijack | projects/embervm/control/lib/embervm/node_registry.ex:1320 |
| `warmth-fetch.unauthorized-source` | physical | `s3-store` → `noded`: fetched artifact bytes on RestoreArtifact | unauthorized-source | the default SeaweedFS S3 gateway is anonymous (no SigV4), so an object noded restores as warmth may have been written by any pod-network caller rather than exported by a legitimate bank; the same-vendor case passes the vendor-stamp check silently | high | anonymous-store-access | projects/embervm/chart/templates/_noded-pod.tpl:283 |

<details>
<summary><b>Not UCAs</b>: 4 examined and rejected</summary>

- **checkpoint-abort auto-bump producing an unblessed generation**: the resolve-timeout auto-abort lane (blessedGeneration: 0) is the ONLY case that produces it, and StatefulStore correctly quarantines it as a fail-closed signal rather than treating it as a bug (noded/server/stateful.go:605-610)
- **no per-principal daily budget configured in the reference deployment**: spend is still bounded by admission caps and concurrency, not unbounded; cutoff is an admission action by design (invariant 4), and quota fails closed the moment a budget IS set (control/lib/embervm/dispatcher.ex:1341, deploy/values.yaml)
- **node-local activator on stateful defaults BlessedGeneration to 0 during control-plane absence**: a zero generation fails pairing and forces a cold boot rather than an incorrect relight, matching invariant 4's fail-open-to-cold-boot rule (noded/server/stateful_activator.go:361)
- **orphan-destroy racing a live node-woken (ACTIVATOR-origin) stateful VM**: adopt_activator_stateful_vms runs on the same reconcile pass before the orphan-destroy loop, and the loop explicitly skips activator_origin? vms as belt-and-suspenders (control/lib/embervm/stateful_manager.ex:2031-2038)
</details>
