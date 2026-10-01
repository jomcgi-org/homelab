# Observability and Alerting

The in-cluster alert templates, alert synchronizer, and notification channel have
been removed. Alerts are Honeycomb triggers (see Alerting below).

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

Alerting is Honeycomb triggers, kept as code in
[`projects/platform/honeycomb/`](../../projects/platform/honeycomb/README.md)
and applied with its `sync.py`. Every trigger notifies the Discord webhook
recipient. There are no in-cluster alert rules. Kubernetes health, ArgoCD
state and Hubble network-policy denials have no trigger.

CNPG backup-age monitoring is staged for Loom only. Monolith-pg needs the same
freshness and checker-absence checks. Trigger specs are applied by hand with
`sync.py`, not by CI; leave the Loom specs disabled until metrics and
test-firing are verified.

The collector and probe configuration is documented in
[`docs/observability.md`](../observability.md).
