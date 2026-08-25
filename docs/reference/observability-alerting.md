# Observability & Alerting

The homelab has a complete observability stack (SigNoz v0.113, OTel, Cilium) with a fully operational alerting pipeline. 33 alert rules monitor infrastructure health, application availability, network policy enforcement, EmberVM safety properties, and GitOps state. Alerts are synced to SigNoz via the signoz-dashboard-sidecar and notify through Incident.io.

## Current State

**What works:** 33 alert rules are synced, evaluating, and generating alert history in SigNoz v0.113. The signoz-dashboard-sidecar reconciles alert ConfigMaps every 5 minutes, creating or updating rules via the SigNoz API. Incident.io notification channel (`incidentio`) is configured via webhook, with Bearer token auth sourced from 1Password.

**Alert inventory:**

- 5 HTTPCheck alerts: monitor service availability via `httpcheck.status` metric
- 7 Kubernetes health alerts: node conditions (disk/memory/PID pressure, readiness), pod health (OOMKilled, pending, restart rate)
- 4 ArgoCD app state alerts: degraded, missing, out-of-sync, suspended
- 14 EmberVM alerts: guest lifecycle, banking, parking, and store-reachability safety properties
- 3 Hubble/Cilium network alerts: node-wide policy-deny drops (warning), fc-invoke L7 5xx rate (warning), monolith policy-denied ingress drops (critical, #4659)

**Remaining gaps:**

- ~17 services lack HTTPCheck alerts (#445, #444)
- No dead man's switch for the httpcheck receiver itself
- Sidecar's own logs are not collected by the OTel collector (uses `slog` to stdout but not instrumented)

## Alert ConfigMap Format (SigNoz v0.113)

All alerts are Kubernetes ConfigMaps with the `signoz.io/alert: "true"` label, discovered and synced by the signoz-dashboard-sidecar. The alert JSON lives in `data.alert.json`.

### Required Fields

```json
{
  "alert": "Alert Name",
  "alertType": "METRIC_BASED_ALERT",
  "ruleType": "threshold_rule",
  "version": "v5",
  "broadcastToAll": false,
  "disabled": false,
  "evalWindow": "10m0s",
  "frequency": "2m0s",
  "severity": "critical",
  "labels": { ... },
  "annotations": { "summary": "...", "description": "..." },
  "condition": { ... },
  "preferredChannels": ["incidentio"]
}
```

### Query Format (v5)

SigNoz v0.113 uses the v5 query builder format with a `queries` array (not the older `builderQueries` map):

```json
"compositeQuery": {
  "queries": [
    {
      "type": "builder_query",
      "spec": {
        "name": "A",
        "signal": "metrics",
        "stepInterval": 60,
        "aggregations": [
          {
            "timeAggregation": "avg",
            "spaceAggregation": "avg",
            "metricName": "httpcheck.status"
          }
        ],
        "filter": {
          "expression": "http.url = 'https://example.jomcgi.dev'"
        },
        "groupBy": [],
        "order": [],
        "disabled": false
      }
    }
  ],
  "panelType": "graph",
  "queryType": "builder"
}
```

Key differences from older SigNoz versions:

- `queries` is an array of `{type, spec}` objects (not a `builderQueries` map of `{queryName, dataSource, ...}`)
- Aggregation uses `timeAggregation`/`spaceAggregation`/`metricName` (not `aggregateOperator`/`aggregateAttribute`)
- Filters use `expression` string syntax (not `items` array with `key`/`op`/`value` objects)

### Threshold Configuration (CRITICAL)

Thresholds must be defined in **two places** on the `condition` object — both are required:

```json
"condition": {
  "compositeQuery": { ... },
  "selectedQueryName": "A",
  "op": "2",
  "target": 1,
  "matchType": "5",
  "targetUnit": "",
  "thresholds": {
    "kind": "basic",
    "spec": [
      {
        "name": "critical",
        "target": 1,
        "targetUnit": "",
        "matchType": "5",
        "op": "2",
        "channels": ["incidentio"]
      }
    ]
  }
}
```

**Why both are required:** SigNoz v0.113's `PostableRule.processRuleDefaults()` defaults the internal `schemaVersion` to `"v1"` (this is separate from the top-level `"version": "v5"` which controls query format). For v1 schema, the threshold evaluation reads from the **legacy condition-level fields** (`op`, `target`, `matchType`, `targetUnit`), not from the `thresholds` block. Without the legacy fields, `BasicRuleThreshold.TargetValue` (`*float64`) is nil, causing a panic during evaluation when a query returns data.

The `thresholds` block is still needed for the SigNoz UI to render threshold configuration correctly.

### Comparison Operators (`op`)

| Value | Meaning      | Use case             |
| ----- | ------------ | -------------------- |
| `"1"` | Greater than | Restart count > N    |
| `"2"` | Less than    | httpcheck.status < 1 |
| `"3"` | Equal to     | Pod phase == Pending |
| `"4"` | Not equal to | Status != expected   |

### Match Types (`matchType`)

| Value | Meaning                                      | Use case                        |
| ----- | -------------------------------------------- | ------------------------------- |
| `"1"` | Once in eval window                          | OOMKilled (any occurrence)      |
| `"3"` | Always in eval window                        | Node pressure (sustained)       |
| `"5"` | N consecutive times (count = eval/frequency) | HTTPCheck (5 failures in 10min) |

## Alert Categories

### HTTPCheck Alerts

Monitor service availability via the OTel httpcheck receiver. Pattern: `max(httpcheck.status)` (space aggregation) where `http.url = '<url>'`, alert when `< 1` for 5 consecutive checks. Uses `max` space aggregation to avoid false positives from stale metric series left by previous collector pod incarnations.

| Service          | URL                                             | Location                      |
| ---------------- | ----------------------------------------------- | ----------------------------- |
| ArgoCD           | `https://private.jomcgi.dev/app/argocd/healthz` | `projects/platform/argocd/`   |
| Longhorn         | `https://private.jomcgi.dev/app/longhorn/`      | `projects/platform/longhorn/` |
| SigNoz           | `https://private.jomcgi.dev/app/signoz`         | `projects/platform/signoz/`   |
| jomcgi.dev       | `https://jomcgi.dev`                            | `projects/platform/signoz/`   |
| Public /health   | `https://jomcgi.dev/health`                     | `projects/platform/signoz/`   |

The public `/health` probe (`public-health-httpcheck-alert.yaml`, critical) watches the
composite health surface rather than page reachability: the frontend proxies
`https://jomcgi.dev/health` to the backend deep check, so a fatal component failure
returns 503 while advisory-only degradation (cd chart lag, red CI) stays at 200 with
`degraded[]` entries and does not trip this alert (#4867).

### ArgoCD App State Alerts

Monitor GitOps application health via the `argocd_app_info` metric. Pattern: `count(argocd_app_info)` where `health_status = '<state>'`, grouped by app name and namespace.

Located in `projects/platform/signoz-addons/alerts/`:

- `argocd-app-degraded.yaml` — health_status = Degraded (warning)
- `argocd-app-missing.yaml` — health_status = Missing (critical)
- `argocd-app-outofsync.yaml` — sync_status = OutOfSync (warning)
- `argocd-app-suspended.yaml` — health_status = Suspended (warning)

### Kubernetes Infrastructure Alerts

Monitor node and pod health via k8s receiver metrics.

Located in `projects/platform/signoz-addons/alerts/`:

- `node-disk-pressure.yaml` — `k8s.node.condition_disk_pressure` > 0 always (warning)
- `node-memory-pressure.yaml` — `k8s.node.condition_memory_pressure` > 0 always (warning)
- `node-pid-pressure.yaml` — `k8s.node.condition_pid_pressure` > 0 always (warning)
- `node-not-ready.yaml` — `k8s.node.condition_ready` < 1 for 5 consecutive (critical)
- `pod-oomkilled.yaml` — `increase(k8s.container.restarts)` where `last_terminated_reason = OOMKilled` > 0 once (critical). Uses `increase` time aggregation to detect new OOM events, not cumulative lifetime count.
- `pod-pending.yaml` — `k8s.pod.phase` == 1 (Pending) for 5 consecutive over 15min (warning)
- `pod-restart-rate.yaml` — `increase(k8s.container.restarts)` > 3 once in 15min (warning). Uses `increase` time aggregation to detect restarts within the eval window, not cumulative lifetime count.

### Network Policy Denial Alerts

Monitor Cilium policy enforcement via Hubble drop metrics. Every `ingress:` CiliumNetworkPolicy in this repo fails closed as a **silent dial timeout**: the denied caller gets no readable error, and the hubble ring buffer is only minutes deep, so drops cannot be audited retroactively (#4659). The labeled `hubble_drop_total` counter series scraped into SigNoz is the durable record. The drop metric carries destination context only because `projects/platform/cilium/values.yaml` requests it (`drop:labelsContext=source_namespace,destination_namespace,destination_workload,traffic_direction`); that list must stay identical to `projects/platform/cilium/bootstrap/cilium-helmchart.yaml` or a node reboot reverts it.

Located in `projects/platform/signoz-addons/alerts/templates/`:

- `hubble-policy-drops.yaml`: node-wide catch-all, `increase(hubble_drop_total)` where `reason = 'POLICY_DENIED'` > 0 once in 5min (warning). Cannot attribute drops; points at live investigation.

Located in `projects/platform/signoz/`:

- `hubble-monolith-policy-drops-alert.yaml`: scoped critical alert (#4659), `increase(hubble_drop_total)` where `reason = 'POLICY_DENIED' AND destination_namespace = 'monolith' AND traffic_direction = 'ingress'`, > 0 for 10 consecutive evaluations over 10min. Namespace-scoped rather than endpoint-scoped because only the monolith app pod carries an ingress CNP in that namespace today; `destination_workload` exists on the series for tightening later once confirmed against a live scrape. Fires only on sustained denial (a rate increase each minute for 10 minutes), never on a single blip.

### SLO-Based Alerts

Services can define SLOs using the `signoz-alerts` library chart (`projects/platform/signoz-addons/alerts/`). Each SLO definition generates two alerts using multi-window multi-burn-rate principles:

1. **Burn-fast** (`<name>-slo-burn-fast`) — Short eval window (5m), fires when the metric sustains a threshold violation. Detects active incidents that would rapidly consume the error budget.

2. **Budget-exhausted** (`<name>-slo-budget-exhausted`) — Long eval window (6h), fires when the metric sustains a threshold violation. Detects slow degradation that has consumed the error budget over time.

SLO definitions live in the consuming chart's `values.yaml`:

```yaml
slos:
  - name: api-gateway
    metric: httpcheck.status
    filter: "http.url = 'https://api.jomcgi.dev/status.json'"
    op: "2" # less than
    threshold: 1
```

The library chart uses `spaceAggregation: "max"` by default to avoid false positives from stale metric series.

To add SLO alerts to a chart:

1. Add `signoz-alerts` as a dependency in `Chart.yaml` (`repository: "file://../signoz-alerts"`)
2. Add `sloDefaults` and `slos` to `values.yaml`
3. Create `templates/slo-alerts.yaml` that ranges over `.Values.slos` and includes `signoz-alerts.slo`

See `projects/monolith/deploy/` for a working example.

## Sidecar Architecture

The `signoz-dashboard-sidecar` (Go, in `projects/platform/signoz-addons/dashboard-sidecar/`) watches for ConfigMaps labeled `signoz.io/alert: "true"` across all namespaces. It:

1. **Watches** — Kubernetes watch on ConfigMaps with the alert label
2. **Reconciles** — Every 5 minutes, force-updates all known alerts (drift correction)
3. **Syncs** — POSTs to create or PUTs to update alerts via SigNoz's `POST /api/v1/rules` API
4. **Tracks state** — Stores `{ConfigMap UID → AlertState(ID, ContentHash)}` in a `signoz-dashboard-sidecar-state` ConfigMap

On 404 (alert deleted from SigNoz), the sidecar recreates the alert automatically.

## Remaining Action Items

1. **Add ~17 missing HTTPCheck alerts (#445, #444)** — Cluster-critical and prod services still need monitoring
2. **Add dead man's switch alert** — Fire when `count(httpcheck.status) == 0` over 10 minutes
3. **Instrument sidecar logging** — Sidecar logs (`slog` to stdout) are not collected by the OTel collector
