# Observability

One OpenTelemetry Collector Deployment on the GKE hub (`homelab-hub`, the only
cluster) exports admitted telemetry to Honeycomb, and a node-level DaemonSet
from the same chart ships warning-and-above pod logs. The hub sends synthetic
probe metrics plus OTLP traces and metrics from the services on its allowlist.
Alerting is one Honeycomb trigger on the public `/health` composite plus
monolith health components that post to Discord (see Alerting below).

## Signal paths

```mermaid
graph LR
    HC[http_check receiver] -->|probe metrics| OC[otel-collector]
    SVC[allowlisted services] -->|OTLP traces and metrics| OC
    OC -->|OTLP over HTTP| H[Honeycomb]
    PODS["/var/log/pods"] -->|warn+ lines| LA[otel-collector-logs DaemonSet]
    LA -->|OTLP, k8s-logs dataset| H
    UR[UptimeRobot] -->|direct HTTPRoute| HEALTH[collector health_check]
```

The collector exports over OTLP/HTTP (`honeycomb.protocol: "http"` in
`values-gke.yaml`): the hub's gRPC dials to `api.honeycomb.io` were cancelled
before completing while HTTPS from the same pod network worked.

The hub Application (`projects/platform-gke/otel-collector/application.yaml`)
applies `values.yaml` then `values-gke.yaml` from
`projects/platform/otel-collector/`. `values-prod.yaml` in the same directory
is the residual home overlay and deploys nowhere.

## Probe targets

The collector's `http_check` receiver probes these targets every 60 seconds and
ships the resulting metrics to Honeycomb:

| Target | Signal |
| ------ | ------ |
| `https://jomcgi.dev/health` | Composite public health |
| `https://jomcgi.dev/` | Public page reachability |
| `https://argocd-server.argocd.svc:443/healthz` | Argo CD, in-cluster, with the pinned serving CA from #6542 |

Targets live under `httpcheck.targets` in `values-gke.yaml`. Public targets use
HTTPS. Stage a new in-cluster target behind a dedicated default-off flag until
the service is live, and revalidate it against the destination's current
reachability and access controls. With the CA mount on, the render fails when
the pinned cert is empty or null. Turning the mount off is not guarded, so keep
it on while the `ca_file` target is enabled. The pinned Argo CD leaf expires
2027-08-30 and must be re-pinned from `argocd-secret` `tls.crt` before then.

The metrics pipeline accepts `http_check` and, when `allowedServices` is
non-empty, the shared OTLP receiver. The allowlist filters traces only.

`loom.enabled` defaults off in all overlays. When enabled after #6605, it
appends the in-cluster query-api `/docs` probe and adds `loom/loom-pg` to the
shared 30-minute CNPG backup checker. Without the shared targets enabled, the
same job checks only Loom. Its ServiceAccount gets only the configured named
Clusters, through namespace-scoped Roles. It emits
`cnpg.backup.last_success_age_seconds` through the collector's OTLP/HTTP Service,
using creation age plus `cnpg.backup.has_successful_backup=false` for a
never-backed-up cluster. Creation age is not evidence of a successful backup.
The flag requires HTTP probes and the shared OTLP receiver to be enabled, and
is independent of the Argo CD CA mount. The rendered upstream Loom chart has no
NetworkPolicies; reachability still needs live validation. Switch the probe
to `/healthz` when weave-hand/loom#694 ships. See
[Loom staged monitoring](../projects/loom/deploy/README.md#staged-monitoring-6604)
for the enable order and live acceptance checks.

## Collector metamonitoring

UptimeRobot checks `https://jomcgi.dev/health/otel-collector`. The `HTTPRoute`
sends traffic directly to the collector's `health_check` extension on port
13133 and rewrites the path to `/`. The public frontend is not on this path.

## Trace admission is deny-by-default

`allowedServices` defaults to an empty list in `values.yaml`. Read
`values-gke.yaml` for the services the hub admits; this document does not list
them, because that list changes and a copy here would go stale.

While the list is empty the rendered collector has:

- no `otlp` receiver
- no traces pipeline
- no container or Service ports for OTLP gRPC on 4317 or OTLP HTTP on 4318

A service that dials the collector without being listed gets connection
refused. The gate is a property of the rendered config, not a convention about
what nobody has pointed at it.

Admitting a service is a one-line `allowedServices` override in
`values-gke.yaml`, using the exact OpenTelemetry `service.name`. A non-empty
allowlist renders the OTLP receiver, trace pipeline, ports, allowlist filter, and
tail sampler. The workload must also configure its exporter endpoint.

Those two edits have to agree. The filter drops any span whose `service.name` is
not on the list, so a service whose `OTEL_SERVICE_NAME` differs from its
allowlist entry exports successfully and has every span discarded, with nothing
reporting the mismatch.

When at least one trace service is admitted, the shared OTLP receiver also feeds
the metrics pipeline. The trace allowlist remains specific to the traces
pipeline.

## Automatic injection is off

Kyverno's cluster-wide OTel environment-variable injection is disabled in
`projects/platform/kyverno/values.yaml`. It was not redirected to the
collector.

The OpenTelemetry Operator chart was retired in #6914.
`projects/platform-gke/` carries no Application for it.
Each workload configures its own exporter endpoint. The private monolith's
OTLP/HTTP trace endpoint is set in `projects/monolith/deploy/values.yaml`, and
the service is admitted through the allowlist above.

The public stats ticker (`/app/notes/stats`) reads a snapshot the
`observability.stats_rollup` job fills from the Kubernetes API, Postgres and
GitHub. The repository removes the DCGM source and GPU presentation in #7028:
the hub has no GPU pool, and the home GPU operator was retired in #6914.
Rollout verification remains on #7028. Snapshot reads and the public read-only
boundary are unchanged.

## Pod logs

A node-level log agent (a DaemonSet in the same chart, enabled on the hub by
`logs.enabled` in `values-gke.yaml`) tails `/var/log/pods` for the namespaces in
`logs.namespaces` and exports warning-and-above lines directly to the Honeycomb
`k8s-logs` dataset, using the same ingest key as the gateway. It does not pass
through the gateway, so it opens no OTLP logs path for workloads.

Levels come from JSON `level`/`severity`/`levelname` fields, a leading level
token on plain-text lines (Python, zap console, Envoy, logfmt), or, for lines
with no level at all, an error/traceback heuristic whose matches are marked
`log.level_source=heuristic`. Everything below WARN is dropped at the node.
Lines carrying `trace_id` pivot to the trace datasets; pod logs from other
namespaces, and anything below WARN, still stay only in the pod logs.

## Alerting

Honeycomb's free plan allows exactly one trigger. It is spent on
`jomcgi.dev /health composite unhealthy` (`aJgkA4vC2m8`), kept as code in
[`projects/platform/honeycomb/`](../projects/platform/honeycomb/README.md)
and applied with its `sync.py`, which refuses to plan more than one trigger.
It fires when the httpcheck probe of `https://jomcgi.dev/health` is non-2xx
for two consecutive 5-minute evaluations and notifies the Discord webhook
recipient "Discord homelab alerts".

Everything else alerts from the monolith. Operational health components in
`projects/monolith/factory/ops_health.py` are registered as **advisory**
components of the private `/api/health`: a failing one is listed under
`degraded` and the response stays 200. They are advisory because they describe
capacity, providers and the factory lane rather than whether the monolith can
serve. The kubelet probes hit `/healthz`, which runs no deep components, so
none of them can restart a pod. They are not composed into the public tier,
so they never move the public `/health` composite or its Honeycomb trigger.

The monolith leader evaluates them every 60 seconds and posts one Discord
message (through `shared.notify`, to `agent.discord.defaultChannelId` unless
`healthAlerts.channelId` is set) when a component flips unhealthy or recovers,
and a reminder every 6 hours while it stays unhealthy
(`projects/monolith/factory/health_alerts.py`). The last announced state
is kept in the `platform_probe` table (`health_alert.<component>`), so a normal
leader handover retains deduplication and reminder timing. State advances after
successful enqueue. A crash between enqueue and the state write can duplicate
a message; this is not an exactly-once delivery guarantee. A component whose data cannot be
read reports `status: unknown`, stays healthy and never causes a transition.
Settings are the chart's `healthAlerts` values.

| Component | Unhealthy when | Data source |
| --------- | -------------- | ----------- |
| `embervm_capacity` | Every EmberVM brick class is at 0 desired replicas, or session creates have failed continuously for more than 15 minutes (failures spanning 15 minutes with no success between) | `noded-brick` Deployments in the `embervm` namespace (monolith ClusterRole read); create outcomes each replica records in `platform_probe` (`embervm.session_create`, `embervm.session_create.failing_since`). Recorded failures: 429, 5xx, timeouts, transport errors. Other 4xx (restore denials) are the caller's and not recorded |
| `agent_turns` | For Codex (luna, terra, sol, astra) or Claude (opus, sonnet, fable, and sessions with no model): at least 2 turns attempted in the last 60 minutes and none delivered | `agent_sessions.agent_turns` joined to `agent_sessions.agent_sessions`. Delivered means a clean terminal reason (`completed`, `end_turn`, `stop`, `user_interrupt`). Interrupted turns and turns cancelled before dispatch are not attempts. Spark and pi are not covered |
| `codex_quota_fresh` | The token broker's latest Codex quota observation is older than 60 minutes (or missing, or the broker is unreachable) **and** at least one Codex turn was attempted in the last 60 minutes | Broker `GET /quota` (the monolith's existing 30-second cached poll) and the same turn counts. The demand condition keeps idle nights quiet; the cost is that a fault that stops Codex work from being attempted at all is left to `agent_turns` and `embervm_capacity` |
| `factory_stuck` | A factory receipt has been `uncertain` for more than 2 hours, or a `queued` receipt at the live policy's repo and generation is not `admission_eligible` (the #6483 trap). Intake receipts while intake is off and older-generation receipts are not flagged | `swarm.factory_receipt` and the `swarm.factory_control` policy. Uncertain age is measured from the receipt's `updated_at` |
| `kargo_promotions` (default off) | At least one stage's latest terminal attempt is Failed or Errored. Unresolved failure, with no age expiry; recovery requires a later Succeeded attempt in that same namespace/stage | Bounded Kubernetes Stage and Promotion lists across all namespaces, including embervm dev and every current pipeline. Creation time orders attempts; Pending, Running and Aborted do not clear a failure. Equal-time conflicting phases cannot recover |
| `embervm_control_errors` (default off) | At least 3 error-or-higher records in the last 900 seconds, summed across selected control-plane pods | Kubernetes logs of container `control-plane`, namespaces `embervm` and `embervm-dev`, selector `app.kubernetes.io/name=embervm`. `Embervm.LogFormatter` JSON `level`, `time`, `message`; includes OTP crashes and explicit manager/router errors, without treating every session-create failure as an error |
| `egress_denials` (default off) | At least 1 explicit egress refusal in the last 900 seconds across selected pods | Kubernetes logs of `egress-proxy` in `embervm` and `embervm-dev`, selector `app.kubernetes.io/component in (noded,noded-brick)`. Go slog JSON `time`, `level`, `msg`; exact `egress denied` or prefix `egress denied:` includes policy refusal, unresolved credentials and unsafe plaintext upstream refusal. Generic 4xx and upstream dial failures do not count |
| `cnpg_backups` (hub enabled) | A fresh checker observation reports no successful backup, even for a newly created cluster, or a successful backup strictly older than 129600 seconds (36 hours). Exactly 36 hours passes | `otel-collector/cnpg-backup-status`, key `observation.json`, written by the same checker that emits the age metric. Required targets: `monolith/monolith-pg`, `authentik/authentik-pg`, `mcp/context-forge-pg`; optional `loom/loom-pg` only when the producer's Loom gate includes it. This checks base-backup status, not WAL archival |

Each check is cached for 60 seconds per process, bounded to 10 seconds with a
5-second Postgres statement timeout, and never raises.

Kubernetes health, ArgoCD state and native network-policy denials remain
uncovered. The three #7003 components replace the disabled #6511 trigger
specifications in repository code only. They are off in the chart defaults,
shared deployment, GKE, dev and recovery overlays. Activation and the live
failure/recovery trial remain on #7003. No Honeycomb keys or triggers are added.
Container restarts in `monolith-public` are posted by the monolith's pod
restart watcher, and chart lag is the advisory `cd` component.

### Staged operational advisories (#7003)

`healthAlerts.operationalSignals.{kargoPromotions,embervmControlErrors,egressDenials}.enabled`
controls each new component independently. The Deployment renders
`KARGO_PROMOTIONS_ENABLED`, `EMBERVM_CONTROL_ERRORS_ENABLED` and
`EGRESS_DENIALS_ENABLED`; the two log target lists become the corresponding
`*_TARGETS` JSON environment values. Runtime consumers are
`factory/advisory_signals.py`, registered through `ops_health.CHECKS`.
A disabled probe reports unknown before consulting its cache, performs no
source reads, sends no notifications and leaves a stored unhealthy alert alone.
The existing `healthAlerts.enabled` still controls the leader-only notifier.
The private module composes these as advisory; the separate public chart and
profile have neither these probes nor Kubernetes grants. `/healthz` is unchanged.

Kargo discovery reads up to 64 Stages and 500 Promotions cluster-wide, each
with a 2 MiB response cap and one page, using the existing get/list ClusterRole
and its binding to the private monolith ServiceAccount. This covers the current
`kargo-monolith`, `kargo-embervm` (dev and prod), `kargo-monolith-agents`,
`kargo-monolith-public` and `kargo-scotland-connector` pipelines without a copied
app list. Ordering uses creationTimestamp, so an old failure completing late
cannot override a newer successful attempt. A queued/running retry leaves the
previous terminal failure unresolved. A stage with no terminal history, invalid
or future timestamps, unknown phases, ambiguous ordering, or a continued list
cannot establish health. A later success clears only its own stage. No age-out
of unresolved failures occurs; deleting a stage removes it from this scope.
Kargo history retention can remove evidence, so this is the unresolved state
visible in retained history, rather than a durable Promotion audit. On a partial
list, observed latest failures remain unhealthy with incomplete coverage; they
may have a superseding success outside the returned page. Recovery requires
complete lists and unambiguous successful terminal evidence for every stage.

Log probes read at most four configured targets and 12 pods total per component,
with three concurrent log requests. Every HTTP read has a 0.8-second wall-clock
deadline, below the enclosing 10-second cached-check deadline. Lists request
12 pods, return at most 2 MiB and never follow continuation tokens. Each log
request asks for 960 seconds of lookback, 2000 lines and 262144 bytes, with
Kubernetes timestamps; the client also caps streamed bytes. The extra 60 seconds
provides a record before the 900-second evaluation boundary. A known window
requires that anchor, a running container started before the lookback, zero
restarts, valid ordered producer JSON with clocks within 60 seconds of the
Kubernetes timestamp, and no line/byte cap or continuation. Missing pods/logs,
malformed or truncated data, restarts, young containers, clock skew, timeout and
403 reads report unknown. A quiet pod without an anchor remains unknown;
rotated logs and historical/deleted pods are not reconstructed. These are
bounded current-pod observations, with no claim of complete fleet audit or
sampling correction. The selected producers emit directly to stdout, without
the collector's warning filter or Honeycomb sampling.

Control-plane errors count at 3 per 15 minutes to tolerate isolated request
errors while detecting repeated infrastructure faults. One egress refusal
counts because even a single rejected guest dependency deserves investigation;
expected deny tests can therefore alert when enabled. Recovery for either log
component requires a complete fresh window below its threshold on all selected
pods. Positive failure counts survive another pod/target's unreadable data;
unknown coverage never sends failure or recovery notices. Health and Discord
include aggregate counts only. Source exceptions log only class and HTTP status,
never API bodies, log text, destinations, tokens or request payloads.
Existing pod list and pods/log get grants suffice; this delivery adds no RBAC,
write permission, secret access or persistence.

**ArgoCD decision.** Neither a broad OutOfSync sweep nor a separate Degraded
component is added. `cluster/cd_health.py` records permanent OutOfSync false
alarms from expected Kargo revision ownership and server-default differences.
A time threshold alone cannot distinguish those from drift. The Kargo
`argocd-wait` step already fails a Promotion on Degraded or its bounded wait
expiry, so the new unresolved-Promotion signal covers that delivery path.
Degradation after a successful Promotion and non-Kargo Applications remain
blind spots. A separate ArgoCD advisory would need explicit resource-diff
exclusions and an application-specific convergence contract before activation.
The existing `cd` chart-lag advisory remains independent.

### Authorized dev trial procedure (not executed)

The landing/validation owner performs this through reviewed GitOps PRs after
repository delivery. Allow up to one working day, and collect all evidence
within 72 hours. Do not mutate live workloads, fake telemetry or bypass
admission. Keep the issue open throughout.

1. Activate the three private `operationalSignals` flags in the intended
   monolith deployment overlay. Verify the delivered revision with
   `verify_deployment`, read private `/api/health` and `/healthz`, and read
   monolith logs for HTTP 403 or source-read warnings. Quiet log sources may
   remain unknown until their coverage anchors exist; record that limitation
   rather than claiming healthy acceptance from missing logs.
2. Prepare a separate reviewed trial chart change that renders a deliberately
   invalid control-plane readiness path only when the release namespace is
   `embervm-dev`. Render both dev and prod before landing: prod must have no
   changed workload manifests. The current readiness path is hardcoded in
   `projects/embervm/chart/templates/deployment.yaml`, so changing an arbitrary
   values key cannot induce this fault. CI must publish a real version of the
   trial chart and Kargo's Warehouse must discover its Freight. Record that
   Freight and version before requesting or observing its dev Promotion.
   `kargo_promote` refuses undiscovered Freight; an invented version is not a
   feasible trial. The failed dev Promotion must never verify that Freight for
   prod; do not use Freight approval to bypass the dev gate.
3. Observe `kargo-embervm/dev` Promotion terminal status by read-only status
   reads, including name, creation and finish timestamps. EmberVM's current
   argocd-wait bound is 1h30m0s, with Degraded able to fail earlier. Stop and
   revert the trial through GitOps if no terminal failure appears within two
   hours. Read the private component after its next 60-second cache refresh
   and capture the delivered unhealthy Discord message ID and timestamp.
4. Revert the dev-only trial template through another reviewed PR. Wait for
   CI publication and discovery of the fixed chart Freight, then observe its
   successful dev Promotion and verified dev rollout. A new running Promotion
   alone cannot recover the advisory. Capture its name/timestamps and the
   delivered recovery Discord message ID/timestamp; confirm unrelated stages
   remain unchanged and the public endpoint stays healthy.
5. Record the five acceptance checklist results on #7003, including the
   delivered monolith revision, fresh known private observations and absence
   of Forbidden reads during the validation interval. If a log component
   remains unknown, report its coverage limitation and leave acceptance open.
   Disable the new flags through GitOps to roll back activation; disabling
   preserves existing unhealthy alert state and emits no synthetic recovery.

## Staged Loom monitoring

Loom's probe and backup-age telemetry remain default-off pending #6605 and
the [staged monitoring checks in #6604](../projects/loom/deploy/README.md#staged-monitoring-6604).
There are no Loom probe, backup-age or checker-absence Honeycomb trigger specs;
the [one-trigger policy](../projects/platform/honeycomb/README.md) reserves the
only trigger for the public health composite. The signals are available for
investigation after enablement and do not create Loom alert notifications.
The shared checker and private backup advisory below are enabled for the three
existing hub clusters under #7002 and can include Loom after its own gate is
enabled. They provide no Loom probe or checker-absence alert.

## Shared hub backup advisory

The hub overlays enable the collector's `backupCheck.enabled` and
`backupCheck.status.enabled`, and the monolith's
`healthAlerts.cnpgBackups.enabled`, for `monolith/monolith-pg`,
`authentik/authentik-pg` and `mcp/context-forge-pg`. Chart defaults and the base
monolith deployment remain opt-in; `loom.enabled` stays false pending #6605.
The existing public Honeycomb trigger is unchanged; no additional trigger is
needed for the Discord advisory. Enabled configuration does not complete the
operational acceptance on #7002.

The checker reads each configured Cluster once, exports that observation, then
patches one precreated ConfigMap through the Kubernetes API with its existing
ServiceAccount token and cluster CA. Its Roles grant only `get` on exact
namespace/name Cluster objects and `get`/`patch` on the single status ConfigMap.
There is no application status-write endpoint, new credential, or database
migration. The private monolith reads that object through its existing
ConfigMap read permission and never scans Clusters itself. The producer
disables redirects and does not log exception bodies or bearer values.

`observation.json` carries a version and per-target identity, observation time,
successful-backup timestamp (or null), creation/backup age, success indication,
and collector acceptance. A failed target does not stop the others. Read or
parse failures become error observations without an age metric. Transport
failure or OTLP partial rejection leaves `export_ok=false`. A known stale or
never-backed-up target still degrades the advisory; a healthy target cannot
clear an alert until its export succeeds. A status patch failure exits nonzero
and leaves the previous snapshot to expire. The next scheduled run retries
collection and publication; there is no in-job retry loop.

Observations are fresh for at most 7200 seconds, with a 60-second per-process
health cache. Missing targets, absent snapshots, stale observations, malformed
fields, future timestamps and unreadable sources report `status: unknown`.
Unknown does not baseline, recover, or update the durable alert state. It also
does not send a new Discord checker-absence alert: inspect the private health
detail and CronJob failures. A known failure on another fresh target still
alerts. Recovery requires fresh valid successful-backup evidence and successful
metric export for every required target and any included Loom target. The
consumer recomputes backup age from the timestamp between checker runs.

One job renders whether shared targets, Loom, or both are enabled. Scheduling,
image and resource settings retain the existing `loom.backupCheck` keys for
compatibility: every 30 minutes, `Forbid` concurrency, no job retries, and a
120-second active deadline. At most four targets make eight read/export
requests and one status patch. Each has a 10-second wall-clock and socket
deadline and a 1 MiB response cap, leaving 30 seconds for startup and parsing.
The hub Application ignores `/data/observation.json` on this exact ConfigMap
during both diff and sync (`RespectIgnoreDifferences=true`). A renamed handoff
must update that ignore rule and both charts together.

The Applications reconcile independently; the reader reports unknown until the
collector publishes a fresh observation. Before accepting the rollout, verify
both Applications are Synced and Healthy, observe the checker complete on its
normal schedule, and read a fresh `observation.json` for all
three targets. Confirm the successful backup timestamps are younger than
129600 seconds and each target has `export_ok=true`; confirm metric arrival in
Honeycomb separately. The live collector Service must still expose port 4318
and the metrics pipeline must retain the shared OTLP receiver.

The checker identity is
`system:serviceaccount:otel-collector:otel-collector-cnpg-backup`. Inspect every
RoleBinding and ClusterRoleBinding applying to that identity or its groups,
including aggregated ClusterRoles. Verify it can get only the three named
Clusters, cannot list/watch Clusters or read an unrelated Cluster, and can
get/patch only `otel-collector/cnpg-backup-status` for its status handoff. Rendered
RBAC proves chart intent; it does not rule out additive grants in the cluster.

Keep #7002 open for these observations, runtime ConfigMap survival across Argo
reconciliation, the approved non-destructive failure/recovery and leader-handover
checks, and health endpoint isolation. Offline contract tests cover stale and
never-backed-up targets, unknown observations, partial failure, failed exports,
normal handover deduplication and recovery without sending live notifications.
Do not inject fake failures into the production status ConfigMap. Keep
`loom.enabled=false` until #6605's independent acceptance is complete, and then
verify it joins the same workload. Revert the three hub enablement flags to
false through Git to disable this path; existing backup schedules are unchanged.

## Configuration

- Collector chart and default admission policy:
  `projects/platform/otel-collector/values.yaml`
- Hub overrides, probe targets, pod-log namespaces and the Honeycomb protocol:
  `projects/platform/otel-collector/values-gke.yaml`
- Honeycomb trigger specs and `sync.py`: `projects/platform/honeycomb/`
- Disabled cluster-wide injection: `projects/platform/kyverno/values.yaml`
- Monolith health components and Discord alerts:
  `projects/monolith/factory/ops_health.py`,
  `projects/monolith/factory/health_alerts.py`, chart values `healthAlerts`
