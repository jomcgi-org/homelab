# Observability and Alerting

The in-cluster alert templates, alert synchronizer, and notification channel have
been removed. Alerts are Honeycomb triggers (see Alerting below).

## Public probes

The OpenTelemetry Collector's `http_check` receiver probes two public URLs every
60 seconds:

| Target | Signal |
| ------ | ------ |
| `https://jomcgi.dev/health` | Composite public health |
| `https://jomcgi.dev/` | Public page reachability |

The collector exports the resulting metrics to Honeycomb. Its metrics pipeline
accepts `http_check` only and does not accept OTLP metrics from services.

Probe targets live under `httpcheck.targets` in
`projects/platform/otel-collector/values-prod.yaml` and
`projects/platform/otel-collector/values-gke.yaml`. Add only public HTTPS URLs.
Revalidate any proposed in-cluster target against the destination's current
reachability and access controls. The removed Cilium policies provide no ingress
enforcement. The GKE overlay additionally enables a live Argo CD in-cluster
target with the pinned serving CA from #6542; with the CA mount on, the render
fails when the pinned cert is empty or null. Turning the mount off is not
guarded, so keep it on while the `ca_file` target is enabled.

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

The collector and probe configuration is documented in
[`docs/observability.md`](../observability.md).
