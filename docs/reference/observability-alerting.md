# Observability and Alerting

The in-cluster alert templates, alert synchronizer, and notification channel have
been removed. Alerting is one Honeycomb trigger on the public `/health`
composite plus monolith health components that post to Discord (see Alerting
below).

## Probe targets

The OpenTelemetry Collector's `http_check` receiver probes two public URLs every
60 seconds:

| Target | Signal |
| ------ | ------ |
| `https://jomcgi.dev/health` | Composite public health |
| `https://jomcgi.dev/` | Public page reachability |

The collector exports the resulting metrics to Honeycomb. Its metrics pipeline
also accepts OTLP when `allowedServices` enables the shared receiver. The
allowlist filters traces only.

Probe targets live under `httpcheck.targets` in
`projects/platform/otel-collector/values-prod.yaml` and
`projects/platform/otel-collector/values-gke.yaml`. Public targets use HTTPS.
Stage in-cluster targets behind a dedicated default-off flag until the service
is live. Revalidate them against the destination's current
reachability and access controls. The removed Cilium policies provide no ingress
enforcement. The GKE overlay additionally enables a live Argo CD in-cluster
target with the pinned serving CA from #6542; with the CA mount on, the render
fails when the pinned cert is empty or null. Turning the mount off is not
guarded, so keep it on while the `ca_file` target is enabled.

`loom.enabled` separately gates the Loom query-api probe and a least-privilege
CronJob reading `loom-pg` backup age. It is off in all overlays pending #6605.
The flag is independent of the Argo CD CA mount. The three disabled Honeycomb
specs cover no successful probe for 600 seconds, backup age above 129600
seconds and missing age data for 7200 seconds. See
[`Loom staged monitoring`](../../projects/loom/deploy/README.md#staged-monitoring-6604)
for the enable order and live acceptance checks.

## Collector metamonitoring

UptimeRobot checks `https://jomcgi.dev/health/otel-collector`. The route reaches
the collector's `health_check` extension directly. It does not pass through the
public frontend.

## Alerting

Honeycomb's free plan allows exactly one trigger. It is spent on
`jomcgi.dev /health composite unhealthy` (`aJgkA4vC2m8`), kept as code in
[`projects/platform/honeycomb/`](../../projects/platform/honeycomb/README.md)
and applied with its `sync.py`, which refuses to plan more than one trigger.
It fires when the httpcheck probe of `https://jomcgi.dev/health` is non-2xx
for two consecutive 5-minute evaluations and notifies the Discord webhook
recipient "Discord homelab alerts".

Everything else alerts from the monolith. Four health components in
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
and a reminder every 6 hours while it stays unhealthy. The last announced state
is kept in the `platform_probe` table (`health_alert.<component>`), so a leader
handover neither repeats nor drops an alert. A component whose data cannot be
read reports `status: unknown`, stays healthy and never causes a transition.
Settings are the chart's `healthAlerts` values.

| Component | Unhealthy when | Data source |
| --------- | -------------- | ----------- |
| `embervm_capacity` | Every EmberVM brick class is at 0 desired replicas, or session creates have failed continuously for more than 15 minutes (failures spanning 15 minutes with no success between) | `noded-brick` Deployments in the `embervm` namespace (monolith ClusterRole read); create outcomes each replica records in `platform_probe` (`embervm.session_create`, `embervm.session_create.failing_since`). Recorded failures: 429, 5xx, timeouts, transport errors. Other 4xx (restore denials) are the caller's and not recorded |
| `agent_turns` | For Codex (luna, terra, sol, astra) or Claude (opus, sonnet, fable, and sessions with no model): at least 2 turns attempted in the last 60 minutes and none delivered | `agent_sessions.agent_turns` joined to `agent_sessions.agent_sessions`. Delivered means a clean terminal reason (`completed`, `end_turn`, `stop`, `user_interrupt`). Interrupted turns and turns cancelled before dispatch are not attempts. Spark and pi are not covered |
| `codex_quota_fresh` | The token broker's latest Codex quota observation is older than 60 minutes (or missing, or the broker is unreachable) **and** at least one Codex turn was attempted in the last 60 minutes | Broker `GET /quota` (the monolith's existing 30-second cached poll) and the same turn counts. The demand condition keeps idle nights quiet; the cost is that a fault that stops Codex work from being attempted at all is left to `agent_turns` and `embervm_capacity` |
| `factory_stuck` | A factory receipt has been `uncertain` for more than 2 hours, or a `queued` receipt at the live policy's repo and generation is not `admission_eligible` (the #6483 trap). Intake receipts while intake is off and older-generation receipts are not flagged | `swarm.factory_receipt` and the `swarm.factory_control` policy. Uncertain age is measured from the receipt's `updated_at` |

Each check is cached for 60 seconds per process, bounded to 10 seconds with a
5-second Postgres statement timeout, and never raises.

Not covered: Kubernetes health, ArgoCD state, Hubble network-policy denials,
and the log-based conditions drafted as disabled trigger specs in #6511
(egress-proxy request denied, EmberVM control-plane errors, Kargo promotion
failed). Container restarts in `monolith-public` are posted by the monolith's
pod restart watcher, and chart lag is the advisory `cd` component.

CNPG backup-age monitoring is staged for Loom only. Monolith-pg needs the same
freshness and checker-absence checks. Trigger specs are applied by hand with
`sync.py`, not by CI; leave the Loom specs disabled until metrics and
test-firing are verified.

The collector and probe configuration is documented in
[`docs/observability.md`](../observability.md).
