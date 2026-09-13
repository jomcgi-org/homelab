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

## EmberVM session API alert contract

The active request telemetry and its Honeycomb query contract are repository
managed. Both queries select:

- `service.name = embervm-control`
- `deployment.environment = homelab-hub`
- `ember.surface = session_api`
- span name `embervm.http.request`

The error-rate query evaluates a five-minute window every minute, requires at
least 20 request spans, and alerts when the proportion with
`http.response.status_code >= 500` is greater than `0.05` (5 percent) for two
consecutive evaluations. Policy denials are 403 responses and intentionally do
not count as server errors.

The p99-latency query evaluates a ten-minute window every minute, requires at
least 10 request spans, excludes
`http.route = /v1/sessions/:id/invoke`, and alerts when
`P99(ember.http.duration_ms) > 300000` milliseconds (five minutes) for two
consecutive evaluations. Invoke duration includes the guest model turn and has
a configured ceiling of 12 hours, so including it would page on legitimate
work rather than control-plane latency. Five minutes is above ordinary cold
session creation while remaining well below the caller's 30-minute create
timeout.

## Alert delivery blocker

This repo configures no in-cluster alert rules or notification channel for the
probe metrics. It also configures no alert rules for Kubernetes health, ArgoCD
state, EmberVM safety properties, or Hubble network-policy denials. The only
Honeycomb credential synchronized by this repository is the ingest-only key
used by the collector. There is no Honeycomb management credential, trigger
reconciler, infrastructure-as-code root, or repository-owned notification
recipient.

Consequently the two query definitions above are not active Honeycomb triggers.
Turning them into deliverable alerts requires a bounded follow-up: choose a
repository-owned Honeycomb trigger reconciler or infrastructure-as-code root,
provision a management key through the existing 1Password Operator pattern, and
name the existing notification recipient. That follow-up must create the two
triggers from the exact fields, units, windows, and thresholds above and verify
a test notification. This repository deliberately does not ship inactive alert
templates meanwhile.

The collector and probe configuration is documented in
[`docs/observability.md`](../observability.md).
