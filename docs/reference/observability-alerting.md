# Observability and Alerting

The in-cluster alert templates, alert synchronizer, and notification channel have
been removed. There is no in-cluster alerting pipeline today.

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
`projects/platform/otel-collector/values-prod.yaml`. Add only public HTTPS URLs.
An in-cluster target also needs a matching Cilium ingress rule on the destination.

## Collector metamonitoring

UptimeRobot checks `https://jomcgi.dev/health/otel-collector`. The route reaches
the collector's `health_check` extension directly. It does not pass through the
public frontend.

## Alerting gap

This repo configures no in-cluster alert rules or notification channel for the
probe metrics. It also configures no alert rules for Kubernetes health, ArgoCD
state, EmberVM safety properties, or Hubble network-policy denials.

The collector and probe configuration is documented in
[`docs/observability.md`](../observability.md).
