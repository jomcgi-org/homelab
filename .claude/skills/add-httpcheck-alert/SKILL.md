---
name: add-httpcheck-alert
description: Create SigNoz HTTP health check alerts for services. Use when adding uptime monitoring for a new service endpoint.
---

# Add SigNoz HTTP Health Check Alert

## Overview

This skill creates a SigNoz HTTP health check alert that monitors service availability via the `httpcheck.status` metric. The alert fires when a service's health check URL fails 5 consecutive times over 10 minutes.

## Workflow

```
┌──────────────────────────────────────────────────────────┐
│  Step 1: Create ConfigMap with Alert Definition          │
│  - Service: jomcgi-dev                                    │
│  - URL: https://jomcgi.dev/health                        │
│  - Label: signoz.io/alert="true"                         │
└────────────────────────┬─────────────────────────────────┘
                         │
                         ▼
┌──────────────────────────────────────────────────────────┐
│  Step 2: Add to Kustomization                            │
│  projects/platform/signoz/kustomization.yaml:              │
│    resources:                                             │
│      - jomcgi-dev-httpcheck-alert.yaml                   │
└────────────────────────┬─────────────────────────────────┘
                         │
                         ▼
┌──────────────────────────────────────────────────────────┐
│  Step 3: Commit and Push to Git                          │
│  - ArgoCD detects change                                 │
│  - Syncs ConfigMap to cluster (5-10 seconds)             │
└────────────────────────┬─────────────────────────────────┘
                         │
                         ▼
┌──────────────────────────────────────────────────────────┐
│  Step 4: SigNoz Alert Operator Discovers ConfigMap       │
│  - Watches for ConfigMaps with signoz.io/alert label     │
│  - Reads alert.json from ConfigMap data                  │
│  - Creates Alert Rule in SigNoz                          │
└────────────────────────┬─────────────────────────────────┘
                         │
                         ▼
┌──────────────────────────────────────────────────────────┐
│  Step 5: HTTP Monitoring Active                          │
│  - SigNoz scrapes httpcheck.status metric               │
│  - If status < 1 for 5 consecutive checks (10 min)      │
│    → Alert fires → incident.io notification               │
│  - If status = 1 (healthy) → No alert                   │
└──────────────────────────────────────────────────────────┘
```

## Arguments

| Argument    | Required | Description                                  | Example                            |
| ----------- | -------- | -------------------------------------------- | ---------------------------------- |
| `service`   | Yes      | Service name (used in metadata and labels)   | `jomcgi-dev`, `argocd`, `longhorn` |
| `url`       | Yes      | Full HTTPS URL for the health check endpoint | `https://jomcgi.dev/health`        |
| `namespace` | No       | Namespace of the owning component            | `signoz`, `argocd`                 |

## File Structure

Create the alert ConfigMap colocated with the platform component that owns the endpoint, and register it in that directory's `kustomization.yaml`. Existing alerts:

```
projects/platform/signoz/jomcgi-dev-httpcheck-alert.yaml
projects/platform/signoz/trips-pages-httpcheck-alert.yaml
projects/platform/signoz/signoz-httpcheck-alert.yaml
projects/platform/argocd/argocd-httpcheck-alert.yaml
projects/platform/longhorn/longhorn-httpcheck-alert.yaml
```

Public-site checks (jomcgi.dev pages) live in `projects/platform/signoz/`, next to the notification channel resource (`incidentio-channel.yaml`).

## ConfigMap Structure

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: <service>-httpcheck-alert
  namespace: <namespace>
  labels:
    signoz.io/alert: "true"
  annotations:
    signoz.io/alert-name: "<Service Name> Unreachable"
    signoz.io/severity: "critical"
    signoz.io/notification-channels: "incidentio"
data:
  alert.json: |
    {
      ... SigNoz alert JSON ...
    }
```

### Required Labels and Annotations

| Field                                         | Value                     | Purpose                              |
| --------------------------------------------- | ------------------------- | ------------------------------------ |
| `labels.signoz.io/alert`                      | `"true"`                  | SigNoz alert operator discovers this |
| `annotations.signoz.io/alert-name`            | `"<Service> Unreachable"` | Human-readable alert name            |
| `annotations.signoz.io/severity`              | `"critical"`              | Alert severity level                 |
| `annotations.signoz.io/notification-channels` | `"incidentio"`            | Notification channel                 |

## SigNoz Alert JSON Format (v0.113+)

```json
{
  "alert": "<Service Name> Unreachable",
  "alertType": "METRICS_BASED_ALERT",
  "ruleType": "threshold_rule",
  "version": "v5",
  "broadcastToAll": false,
  "disabled": false,
  "evalWindow": "10m0s",
  "frequency": "2m0s",
  "severity": "critical",
  "labels": {
    "service": "<service>",
    "environment": "production"
  },
  "annotations": {
    "summary": "<Service Name> at <url> is unreachable",
    "description": "HTTP health check has failed 5 consecutive times over 10 minutes. This could indicate the service is down or Cloudflare auth is failing (3xx redirect)."
  },
  "condition": {
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
              "expression": "http.url = '<url>'"
            },
            "groupBy": [],
            "order": [],
            "disabled": false
          }
        }
      ],
      "panelType": "graph",
      "queryType": "builder"
    },
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
  },
  "preferredChannels": ["incidentio"]
}
```

### Key Alert Parameters

| Parameter    | Value   | Description                                     |
| ------------ | ------- | ----------------------------------------------- |
| `evalWindow` | `10m0s` | Time window to evaluate the condition           |
| `frequency`  | `2m0s`  | How often to check the condition                |
| `version`    | `"v5"`  | Alert schema version (required for v0.113+)     |
| `matchType`  | `"5"`   | Alert when condition met 5 times in eval window |
| `op`         | `"2"`   | Less than (numeric enum, see table below)       |
| `target`     | `1`     | httpcheck.status = 1 means success, 0 = failure |

### Threshold Fields (CRITICAL)

Thresholds must be defined in **two places** — both are required for SigNoz v0.113:

1. **Legacy fields** on the `condition` object: `op`, `target`, `matchType`, `targetUnit`
2. **`thresholds` block** with `kind: "basic"` and a `spec` array

Both must have matching values. SigNoz v0.113 defaults `schemaVersion` to `"v1"` internally,
which reads thresholds from the legacy condition-level fields. Without them, threshold
evaluation panics with a nil pointer dereference (`BasicRuleThreshold.TargetValue` is `*float64`).

### Comparison Operators (`op`)

| Value | Meaning      |
| ----- | ------------ |
| `"1"` | Greater than |
| `"2"` | Less than    |
| `"3"` | Equal to     |
| `"4"` | Not equal to |

### Match Types (`matchType`)

| Value | Meaning                                      |
| ----- | -------------------------------------------- |
| `"1"` | Once in eval window                          |
| `"3"` | Always in eval window                        |
| `"5"` | N consecutive times (count = eval/frequency) |

## Usage Examples

### Example 1: Public site health endpoint

```
/add-httpcheck-alert jomcgi-dev https://jomcgi.dev/health signoz
```

Creates: `projects/platform/signoz/jomcgi-dev-httpcheck-alert.yaml`

### Example 2: Platform component with its own namespace

```
/add-httpcheck-alert argocd https://private.jomcgi.dev/app/argocd argocd
```

Creates: `projects/platform/argocd/argocd-httpcheck-alert.yaml`

## Post-Creation Steps

1. Add the alert file to the service's `kustomization.yaml` if not using `resources: ["*.yaml"]`
2. Commit and push the changes
3. ArgoCD will sync the ConfigMap
4. SigNoz alert operator will create the alert rule

## Verification

After ArgoCD syncs, verify the alert was created:

```bash
kubectl get configmap <service>-httpcheck-alert -n <namespace>
```
