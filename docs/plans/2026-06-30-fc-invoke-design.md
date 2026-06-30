# fc-invoke: a single configurable surface for running workloads in Firecracker

**Author:** jomcgi (with Claude)
**Date:** 2026-06-30
**Status:** Design, pending plan
**Related ADRs:** [019 Substrate Executor](../decisions/agents/019-substrate-executor-agentworkflow.md), [022 FC Snapshot/Restore Controller](../decisions/agents/022-firecracker-snapshot-restore-controller.md), [023 Egress Secret Proxy](../decisions/agents/023-egress-secret-proxy.md), [025 Three-Layer Agent Stack](../decisions/agents/025-three-layer-agent-stack-goosecracker.md), [026 Fast MicroVM Starts](../decisions/agents/026-fast-microvm-starts-and-stateful-artifact-iteration.md)

## Problem

Two independent host daemons run workloads in Firecracker microVMs and each rolls its own orchestration around the shared `fcvm/driver`:

- `semgrep-scand` boots/restores a guest, sends a scan over a vsock side-channel (ScanPort), discards the VM. Warm base, concurrency semaphore, restore-discard.
- `fc-agentd` boots/restores a guest, runs goose over the control channel, snapshots on idle. Warm base, concurrency cap, restore-keep.

The genuinely shared lifecycle (Claim/Snapshot/Restore/Release) already lives in `fcvm/driver`, which implements `substrate.Substrate` + `substrate.Snapshotable`. What is duplicated, and what is missing, is narrower than it first appears:

1. **`Substrate.Exec` is an unimplemented stub.** ADR 019 specified `Exec(ctx, Handle, Request) (Stream, error)` as "run work, stream output." It was never implemented; each consumer rolls its own request transport over vsock (semgrep over ScanPort, the agent over ControlPort Assign). The generic "invoke a function in the VM, get a result" primitive, Lambda 101, was designed and then stubbed.
2. **The host orchestration around that transport is written twice** with different structure: warm-base build/invalidation, the concurrency cap (`semaphore.Weighted` in scanner, `MaxConcurrent` in reconcile), and restore-do-work-release.

Running an HTTP service in a microVM and calling it is a common, reusable pattern beyond the agent use case. We want one configurable surface for it, not N daemons.

## Decision summary

Build **fc-invoke**, a single host daemon that is the implementation of ADR 019's `Exec` seam plus the orchestration around it, configured declaratively per workload. It absorbs both `semgrep-scand` and `fc-agentd`'s VM lifecycle, leaving thin consumers above it.

### Surface and transport

- **One daemon with an HTTP ingress.** External callers (monolith MCP tool, the Discord bot, a future CI webhook) `POST /invoke/{workload}[/{session}]` with a payload. fc-invoke boots/restores the right VM, proxies the request, returns the result. One process owns warm-base, concurrency, and transport.
- **HTTP over vsock.** The guest runs an HTTP server bound to a vsock port; fc-invoke reverse-proxies the inbound request to it. Streaming is free (a chunked response body covers both semgrep's unary result and the agent's streamed output). The guest contract is simply "be an HTTP server on a vsock port." Both ends of the transport are generic: a host-side reverse proxy and a guest-side shim, a matched pair living in the substrate layer.

### Configuration: named workloads in Helm values

A workload is defined entirely by generic, server-side, reviewable values. A caller references a workload by name and sends a payload; it never picks the image or secrets (which keeps ADR 023's server-side secret model intact). The full primitive set, seven generic knobs, none of which would change to run a non-goose workload:

```yaml
# firecracker/deploy/values.yaml
workloads:
  semgrep:
    image: semgrep-guest # logical name; digest pinned by Bazel helm_images_values
    resources: { vcpu: 2, mem: 2Gi }
    concurrency: 4
    egress: { enabled: false }
    warmBase: { build: true, readyPath: /shim/ready } # boot, wait for ready, snapshot
    sessioned: false # no session path segment
    requestTimeout: 90s
  artifact:
    image: agent-guest
    resources: { vcpu: 4, mem: 4Gi }
    concurrency: 2
    egress: { enabled: true, secrets: [openrouter] } # credential injection + egress gate
    warmBase: { build: true }
    sessioned: true # gets a /{session} correlation segment
    requestTimeout: 180s
```

`image, resources, concurrency, egress{enabled,secrets}, warmBase{build,readyPath}, sessioned, requestTimeout`. Nothing names goose, sessions.db, or git.

### Two independent lifecycle primitives

Snapshot/restore and session-resume are orthogonal and serve different jobs:

| Primitive           | Captures                  | Mechanism                                                                   | Lives in                                                   | Used by                                                           |
| ------------------- | ------------------------- | --------------------------------------------------------------------------- | ---------------------------------------------------------- | ----------------------------------------------------------------- |
| **Warm base**       | workload-generic env prep | VM snapshot taken once after a readiness-gated "prime", restored per invoke | platform (fcvm/driver.SnapshotBase)                        | semgrep (compiled rules, warm LSP); future bazel (analysis cache) |
| **State hydration** | per-session caller state  | pre-run pull + post-run push of external state into a disposable VM         | guest shim capabilities, parameterized by the orchestrator | coding/artifact agents (goose sessions.db, git mirror)            |

The line between them is _who captures the state_. A warm base is a VM snapshot, a platform mechanism, gated on a generic readiness probe (the guest decides what "ready" means; the platform does the capture). Hydration is per-session state movement to external storage that only the guest can interpret, so it stays out of the platform.

### All durable state is owned by orchestrators (2-way door)

fc-invoke holds **no durable state**. The orchestrator (goosecracker, the monolith) owns the session registry and all session content (its existing `claude_agent` tables plus the S3 blobs and the git mirror). fc-invoke owns only session _routing_ (the `/{session}` path segment as an opaque correlation key for in-flight de-dup and tracing) and ephemeral runtime state (the concurrency semaphore, live VM handles). So fc-invoke is a stateless daemon: warm-base manager + concurrency cap + egress gate + proxy.

This is a deliberate, reversible choice. The substrate being stateless makes the door cheap: adding substrate-owned per-session snapshots or state later is a new capability behind the unchanged `/invoke` contract, not a migration.

### The extensible shim

The guest-side half of the substrate is a shared Go library, baked into guest images by Bazel. It can be rich without leaking into the public interface, because the fc-invoke _daemon_ never imports it; only guest images do.

```
substrate/shim/
├── server          # HTTP-over-vsock server (identical for all guests)
├── hooks           # pre-run / post-run hook chain around the workload handler
├── capabilities/   # reusable, WORKLOAD-AGNOSTIC building blocks
│   ├── git         #   clone / pull a mirror at a ref
│   └── objectstore #   pull / push a blob (S3) by key
└── handler         # interface a workload implements: Handle(req) -> resp
```

A guest image = `shim + enabled capabilities + a workload handler`. Capabilities are generic (git, object-store); the goose-specific composition ("pull sessions.db from `s3://.../sessions/<id>.db`, clone the repo mirror") lives in the agent-guest _handler_, which uses those capabilities. Even the shim library stays workload-agnostic.

### API namespacing

Three tiers, each owned by a different layer (the layering made visible in the URL):

| Path                             | Owner            | Semantics                                                                                                                                                          |
| -------------------------------- | ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `/invoke/{workload}[/{session}]` | substrate        | The standard contract: boot/restore, enforce concurrency + timeout, proxy the body to the guest handler, return the response. The only path the substrate defines. |
| `/shim/*`                        | shim library     | Common capability/control endpoints in every guest that links the shim (`/shim/healthz`, `/shim/ready`).                                                           |
| anything else                    | workload handler | Workload-defined routes, transparently proxied; the substrate forwards, never interprets.                                                                          |

Orchestrators "pass stuff in" through the **opaque request body** on `/invoke/`: it carries directives (git ref, session blob key, the task) that the shim capabilities and handler consume. The substrate never parses the body, so a new capability needs zero substrate change.

## Directory layout

```
projects/firecracker/
├── substrate/                  # the platform everything runs on — one project, one chart
│   ├── fcvm/                   #   VM driver: Claim/Snapshot/Restore/Release (internal engine)
│   ├── vsockproto/             #   vsock transport
│   ├── egress-proxy/           #   egress secret-swap (ADR 023)
│   ├── shim/                   #   shared guest-side HTTP-over-vsock server + capabilities (bazel-baked)
│   ├── invoke/                 #   Substrate iface (internal seam + in-mem fake) + fc-invoke daemon
│   ├── chart/                  #   the one Helm chart: fc-invoke Deployment, RBAC, OnePasswordItems
│   └── deploy/                 #   ArgoCD Application (pinned targetRevision) + values.yaml + kustomization
├── goosecracker/               # thin stateful agent layer: session<->thread map, Discord wake, business state
│   └── recipes/                #   artifact.yaml, code-review.yaml — each agent is a recipe + values entry
└── semgrep/                    # semgrep guest image + handler + warm-base prime; an fc-invoke workload
```

This is ADR 025's `agents/{firecracker-substrate, goosecracker, discord}` shape with three corrections this design justifies:

1. `firecracker-substrate` absorbs fc-invoke and shortens to `substrate`; the home is `projects/firecracker/` (grouping the microVM family; bare `agents/` collides with `goosecracker/recipes`, and the `fc-` prefix is a directory doing a string's job).
2. `semgrep` is a peer workload dir, not a sub-item; it is the non-goose proof that the substrate is harness-agnostic (ADR 025 open question 4, answered).
3. `discord` drops as a directory: it has no distinct guest image (it runs the same goose harness, and ADR 026 folds artifact iteration into it), so it is a goosecracker values config plus the bot glue already in `projects/monolith/chat/`. A directory whose only job is "build one image" is not worth it until there is a distinct image (YAGNI; deviation from ADR 025 decision 4).

Guest images are **not** Kubernetes pods. They are OCI images built by each subdir's apko, pushed to GHCR, seeded onto node-4's devmapper thin-pool, and booted as microVMs by fc-invoke. The chart templates the _workload config_ fc-invoke reads, not a Deployment per workload. The Bazel `helm_images_values` rule deep-merges build-time-pinned digests into `values.yaml`, so `image: semgrep-guest` becomes `semgrep-guest@sha256:...` at chart-build and the workload auto-rolls when its guest rebuilds. The whole family deploys and revs as one unit.

## Data flow

### A. Stateless warm-base invoke (semgrep)

```
monolith MCP --POST /invoke/semgrep {files}--> fc-invoke
  acquire concurrency slot
  Restore VM from semgrep warm-base snapshot (~tens of ms; rules already compiled)
  reverse-proxy body --vsock--> guest shim --> semgrep handler (LSP scan)
  stream findings back; Release (discard) VM; free slot
```

No session, no hooks, no egress. Pure lambda.

### B. Session invoke with hydration (code-review agent)

```
Discord reply --> goosecracker: map thread -> session t-abc, pick recipe code-review
  --POST /invoke/code-review/t-abc {task, gitRef, sessionKey}--> fc-invoke
  acquire slot
  Restore VM from agent warm-base; inject egress + [github, openrouter] placeholders
  reverse-proxy body --vsock--> guest shim
    PRE hooks (from body directives):
      git capability:         clone mirror @ gitRef
      objectstore capability: pull sessions.db by sessionKey
    code-review handler: goose --resume over hydrated state, edits, streams output
      (egress calls -> placeholder swapped at egress hop, ADR 023)
    POST hooks:
      objectstore capability: push updated sessions.db
      git capability:         push claude/ branch to mirror
  stream result back; Release VM; free slot
  (orchestrator marks its own session row IDLE; fc-invoke keeps nothing)
```

The substrate did four generic things (slot, restore-from-base, inject egress creds, proxy). Everything goose- or repo-specific happened inside the guest via shim capabilities the orchestrator parameterized through the body.

## Error handling

Durable state is only ever written by the guest's post-run hook on success, so every substrate-level failure leaves it untouched and retries resume from the last committed point.

| Failure                           | fc-invoke behavior                                                                                                | Durable state              |
| --------------------------------- | ----------------------------------------------------------------------------------------------------------------- | -------------------------- |
| At capacity                       | queue for the slot, bounded; 503 on queue timeout                                                                 | none                       |
| Warm-base missing / restore fails | fall back to cold-boot the base rootfs (snapshot is a latency optimization, never load-bearing)                   | none                       |
| Boot fails entirely               | 503 `GuestUnavailable`                                                                                            | none                       |
| Hydration fails (guest-side)      | proxied through as the handler's own error (a workload error, not `GuestUnavailable`); orchestrator decides retry | none (push never reached)  |
| Egress target down                | handler error in the response body; substrate does not special-case                                               | none                       |
| Request timeout                   | kill VM, 504                                                                                                      | none (post-hook never ran) |
| VM crash mid-run                  | detect, 5xx, discard VM                                                                                           | none                       |

The distinction that matters for the orchestrator: `GuestUnavailable` (503, substrate could not give you a VM, retriable infra) vs a handler error (the VM ran, the workload failed, the workload's problem). This is the split semgrep-scand draws today, generalized.

## Testing

- **In-memory substrate fake** (exists) - consumer tests with no cluster.
- **Shim library** - unit-test the hook chain + handler with fake capabilities (fake git, fake object-store); no VM, no real S3.
- **fc-invoke daemon** - unit-test proxy + concurrency + warm-base orchestration with a fake driver and fake guest transport (the existing `vmDriver`/`guestTransport` fake pattern). No Postgres needed (state-free); session-registry tests stay in the orchestrator.
- **node-4 end-to-end** - real warm-base build + restore continuity where `/dev/kvm` exists. CI covers logic; node-4 covers FC mechanics. No local test loop (BuildBuddy CI on push).

## Migration: semgrep first, then agent

Each step is independently shippable.

1. **fc-invoke daemon + stateless invoke path.** Warm base, concurrency, HTTP-over-vsock proxy, egress toggle. Plus the shared shim (server + capabilities) and the in-memory fake.
2. **semgrep cutover.** `semgrep-guest` gains the shim HTTP handler; the `semgrep` workload config lands; the `monolith-semgrep-scan` MCP tool re-points from `SEMGREP_SCAND_URL` to `/invoke/semgrep`. Delete `semgrep-scand`'s host orchestration. Lowest risk: stateless, no Discord/Postgres coupling, already has a clean HTTP MCP wrapper. Proves the daemon + guest HTTP contract in prod.
3. **fc-invoke session routing + warm-base for the agent workload.** The `/{session}` correlation segment, agent warm-base.
4. **agent cutover.** `agent-guest` gains the shim handler + git/object-store hydration composition; the agent workloads (artifact, code-review) land as configs; thin `fc-agentd` down to the orchestrator role (session<->thread map, Discord wake), with its VM lifecycle removed in favor of fc-invoke calls.
5. **Rename + ADR.** Move dirs to `projects/firecracker/{substrate, goosecracker, semgrep}`; update ADR 025 to Accepted with this design folded in.

## Open questions (settled during execution, not gates)

1. Per-session serialization (two Discord replies racing the same session's external state): the orchestrator's call, since it owns the state. fc-invoke enforces only per-workload resource concurrency. Confirm the orchestrator serializes before two writers can clobber a sessions.db push.
2. The exact `/shim/*` control surface (readiness, capability introspection, metrics) - grow as needed; `/shim/ready` is the only one warm-base requires.
3. Whether the shim hook chain is configured per-image (baked manifest) or per-request (body directives) or both. Leaning both: image declares which capabilities are available, the body parameterizes them.
4. CI webhook HTTP consumer (ADR 025 trigger wiring) is now trivial on top of `/invoke/` but remains a follow-on, not part of this migration.
