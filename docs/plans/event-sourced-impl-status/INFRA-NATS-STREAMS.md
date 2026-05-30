# INFRA-NATS-STREAMS — status

Unit of the additive event-sourced lakehouse build. Deploys the NATS JetStream
controller (nack) and four declarative `Stream` Custom Resources for the ADR
agents/016 + 017 event bus.

## What shipped

Purely additive — two new directories, no existing file modified:

- `projects/platform/nack/` — Helm Application wrapping the upstream nack chart.
- `projects/platform/nats-streams/` — kustomize Application (no Helm) emitting four `Stream` CRs.

### nack controller

| Item                  | Value                                                               |
| --------------------- | ------------------------------------------------------------------- |
| Upstream chart        | `nack`                                                              |
| Chart version         | **0.34.0** (latest published)                                       |
| appVersion / image    | `natsio/jetstream-controller:0.23.0`                                |
| Helm repo             | `https://nats-io.github.io/k8s/helm/charts/`                        |
| Wrapper chart version | `1.0.0` (homelab `projects/platform/nack/Chart.yaml`)               |
| ArgoCD sync-wave      | `1` (CRDs land before the Stream CRs)                               |
| Destination namespace | `nats`                                                              |
| Controller NATS URL   | `nats://nats.nats.svc.cluster.local:4222` (rendered into `-s=` arg) |

nack values are nested under the `nack:` key (it's a dependency subchart). Notable
overrides: `useLegacyNames: false` (modern labels for subchart use),
`namespaced: false` (watch CRs cluster-wide), `podAnnotations.config.linkerd.io/opaque-ports: "4222"`
(binary NATS protocol), non-root securityContext (uid 65532), read-only rootfs
container security context (the chart mounts a `runtime` emptyDir at `/nack` so
this works), and small resource requests/limits.

The CRDs (`streams|consumers|accounts|...jetstream.nats.io`) ship in the chart's
Helm `crds/` directory. `helm template` does not render that dir, but ArgoCD
applies it automatically. The Application sets `ServerSideApply=true` to avoid the
"metadata.annotations too long" failure that large CRDs hit with client-side apply.

### Stream CRD

- **apiVersion:** `jetstream.nats.io/v1beta2` (the served + storage version in chart 0.34.0).
- **Server association:** each `Stream.spec.servers` is a list of NATS URLs. We set
  `servers: ["nats://nats.nats.svc.cluster.local:4222"]` on every stream (no Account
  abstraction). The controller's own `jetstream.nats.url` is also set as the default
  for any future resource that omits `spec.servers`.
- Dedup window field is named **`duplicateWindow`** (not `duplicates`); discard enum
  value is `old`; retention enum value is `limits`. `maxAge` is a Go-duration string,
  empty/omitted = unlimited; `maxBytes` is an integer (bytes); `replicas` an integer.

### The four streams

All share: `storage: file`, `replicas: 1` (single-node NATS, cluster disabled),
`retention: limits`, `discard: old`, `maxAge` omitted (**infinite retention** per
ADR 016 Open Question 1 — file backend, replay from history), `duplicateWindow: 2m`
(supports ADR 017 `Nats-Msg-Id = {entity_id}-v{version}` dedup), and
`maxBytes: 5368709120` (**5 GiB** per-stream storage-isolation cap).

| Stream             | subjects             | Covers (ADR 016 taxonomy)     |
| ------------------ | -------------------- | ----------------------------- |
| `events-knowledge` | `events.knowledge.>` | gap, note, edge               |
| `events-serving`   | `events.serving.>`   | artifact-ready                |
| `events-ingest`    | `events.ingest.>`    | email-arrived, calendar-event |
| `events-ops`       | `events.ops.>`       | alert-fired, build-completed  |

Storage budget: 4 × 5 GiB = 20 GiB against the NATS 50 Gi PVC, leaving headroom for
the existing `ais` stream and JetStream overhead. Bump per-stream `maxBytes` later if
a domain grows; infinite age means `maxBytes` is the only ceiling.

## Layout note (deviation)

The nats-streams Application uses plain kustomize, so the directory's
`kustomization.yaml` can serve only one role. Resolved by mirroring the
`*/deploy` split: top-level `nats-streams/kustomization.yaml` lists `application.yaml`
(app-discovery, what the parent platform kustomization loads), and the Stream CRs
live in `nats-streams/streams/` with their own `kustomization.yaml`. The Application's
`path` points at `projects/platform/nats-streams/streams`. A single shared
kustomization can't both emit the Application and the Stream CRs.

nats-streams has **no BUILD file** — matching other plain-kustomize platform dirs
(`cloudnative-pg/deploy`, `atlas-operator/deploy`). nack has a BUILD with
`helm_chart` + `argocd_app` mirroring `projects/platform/nats/BUILD`, including the
same `semgrep_exclude_rules` (upstream chart doesn't expose every security knob on
all generated resources).

## Deferred / out of scope

- **Central wiring** — `projects/platform/kustomization.yaml` (add `./nack` + `./nats-streams`)
  and `projects/home-cluster` are handled by the orchestrator after merge, per unit
  conventions. Not touched here.
- **Consumers** — no `Consumer` CRs yet; dispatchers (ADR 016) subscribe later.
- **Per-subject NATS accounts / authorization** (ADR 016 future work) — using a single
  shared connection for now; no Account CRs.
- **TLS to NATS** — disabled (in-cluster, Linkerd mTLS on the mesh path).

## Validation performed

- `kubectl kustomize projects/platform/nats-streams/` → renders the `nats-streams` Application.
- `kubectl kustomize projects/platform/nats-streams/streams/` → renders all 4 Stream CRs.
- `helm dependency build projects/platform/nack/` → fetched nack 0.34.0 (Chart.lock + charts/ vendored).
- `helm template nack projects/platform/nack/ -f .../values.yaml` → renders cleanly; NATS URL,
  resources, security contexts, and Linkerd opaque-port annotation all applied.
- Repo `format` run reverted on pre-existing files (full-repo prettier drift); new
  files were already conformant. Nothing applied to the cluster; no `bazel test`.
