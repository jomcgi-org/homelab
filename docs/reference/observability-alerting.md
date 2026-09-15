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
managed. Honeycomb trigger queries with named calculations cannot combine those
calculations with global filters, so every named calculation repeats these
filters:

- `service.name = embervm-control`
- `deployment.environment = homelab-hub`
- `ember.surface = session_api`
- span name `embervm.http.request`

The error-rate trigger contract is:

| Setting | Value |
| ------- | ----- |
| Query `time_range` | 240 seconds |
| Trigger `frequency` | 120 seconds |
| Named calculation `requests` | `COUNT(http.response.status_code)` |
| Named calculation `errors` | `SUM(is_server_error)`, where the query-scoped field is 1 for status 500 or greater and 0 otherwise |
| Named calculation `saturation` | `COUNT(ember.observation.saturated)` |
| Formula | `($errors / $requests) / (($saturation * 1000000) + 1)` |
| HAVING guard | `COUNT(http.response.status_code) >= 20` |
| Threshold | greater than `0.05` for 2 consecutive evaluations |

The status field is absent on saturation markers, so `requests` is the request
denominator. A zero denominator makes the ratio nil and Honeycomb skips that
evaluation. Policy denials are 403 responses and do not count as server errors.
When a saturation marker exists, the formula's divisor makes the maximum result
less than `0.000001`, explicitly suppressing the invalid window. At a two-minute
frequency, two consecutive evaluations mean four minutes of sustained breach.

The p99-latency trigger contract is:

| Setting | Value |
| ------- | ----- |
| Query `time_range` | 600 seconds |
| Trigger `frequency` | 300 seconds |
| Named calculation `eligible_requests` | `COUNT(ember.http.duration_ms)` with invoke routes excluded |
| Named calculation `latency` | `P99(ember.http.duration_ms)` with invoke routes excluded |
| Named calculation `saturation` | `COUNT(ember.observation.saturated)` |
| Formula | `$latency / (($saturation * 1000000) + 1)` |
| HAVING guard | `COUNT(ember.http.duration_ms) >= 10`, with the same invoke exclusion |
| Threshold | greater than `300000` milliseconds for 2 consecutive evaluations |

The invoke exclusion is applied before both the percentile and its minimum-count
guard. Invoke duration includes the guest model turn and has a configured
ceiling of 12 hours, so including it would page on legitimate work rather than
control-plane latency. Five minutes is above ordinary cold session creation
while remaining below the caller's 30-minute create timeout. Saturation markers
remain included in their named calculation, so an invoke burst conservatively
suppresses the latency evaluation too. At a five-minute frequency, two
consecutive evaluations mean ten minutes of sustained breach.

Both pairs satisfy Honeycomb's current trigger constraint
`frequency <= time_range <= min(4 * frequency, 86400)`, use minute-multiple
frequencies, and stay within the API's maximum of five consecutive evaluations.
The formulas use the single formula permitted by the trigger schema. The HAVING
clause supplies the single permitted low-traffic guard.

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
