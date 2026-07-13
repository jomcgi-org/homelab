{{/*
signoz-alerts.slo generates two SigNoz alert ConfigMaps for an SLO definition.

Usage:
  {{- include "signoz-alerts.slo" (dict "slo" $sloEntry "Chart" .Chart "Release" .Release "defaults" $.Values.sloDefaults) }}

Required fields in .slo:
  - name: string (alert name prefix, e.g., "api-gateway")
  - metric: string (SigNoz metric name, e.g., "httpcheck.status")
  - filter: string (SigNoz filter expression, e.g., "http.url = 'https://...'")

Optional fields in .slo (with defaults from .defaults):
  - op: string (comparison operator, default "2" = less than)
  - threshold: number (value to compare against, default 1)
  - severity: string (default "critical")
  - channels: list (default ["incidentio"])
  - groupBy: list of {name, fieldDataType, fieldContext} (default [])
  - spaceAggregation: string (default "max")
  - timeAggregation: string (default "avg")
*/}}
{{- define "signoz-alerts.slo" -}}
{{- if not .slo.name }}{{ fail "signoz-alerts.slo: .slo.name is required" }}{{ end }}
{{- if not .slo.metric }}{{ fail "signoz-alerts.slo: .slo.metric is required" }}{{ end }}
{{- if not .slo.filter }}{{ fail "signoz-alerts.slo: .slo.filter is required" }}{{ end }}
{{- $slo := .slo }}
{{- $defaults := .defaults }}
{{- $severity := $slo.severity | default $defaults.severity | default "critical" }}
{{- $channels := $slo.channels | default $defaults.channels | default (list "incidentio") }}
{{- $op := $slo.op | default "2" }}
{{- $threshold := ternary $slo.threshold 1 (hasKey $slo "threshold") }}
{{- $spaceAgg := $slo.spaceAggregation | default "max" }}
{{- $timeAgg := $slo.timeAggregation | default "avg" }}
{{- $groupBy := $slo.groupBy | default list }}
{{- $environment := $slo.environment | default $defaults.environment | default "production" }}
{{- $burnFast := $defaults.burnFast | default dict }}
{{- $budgetExhausted := $defaults.budgetExhausted | default dict }}
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ $slo.name }}-slo-burn-fast
  labels:
    app.kubernetes.io/managed-by: {{ .Release.Service }}
    app.kubernetes.io/instance: {{ .Release.Name }}
    helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
    signoz.io/alert: "true"
    signoz.io/alert-type: "slo-burn-fast"
  annotations:
    signoz.io/alert-name: {{ printf "%s SLO Burn Rate High" $slo.name | quote }}
    signoz.io/severity: {{ $severity | quote }}
    signoz.io/notification-channels: {{ join "," $channels | quote }}
data:
  alert.json: |
    {
      "alert": {{ printf "%s SLO Burn Rate High" $slo.name | quote }},
      "alertType": "METRIC_BASED_ALERT",
      "ruleType": "threshold_rule",
      "version": "v5",
      "broadcastToAll": false,
      "disabled": false,
      "evalWindow": {{ $burnFast.evalWindow | default "5m0s" | quote }},
      "frequency": {{ $burnFast.frequency | default "1m0s" | quote }},
      "severity": {{ $severity | quote }},
      "labels": {
        "service": {{ $slo.name | quote }},
        "alert_type": "slo_burn_fast",
        "environment": {{ $environment | quote }}
      },
      "annotations": {
        "summary": {{ printf "%s is burning through its error budget rapidly" $slo.name | quote }},
        "description": "High burn rate detected — at this rate the error budget will be exhausted well before the SLO window ends."
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
                    "timeAggregation": {{ $timeAgg | quote }},
                    "spaceAggregation": {{ $spaceAgg | quote }},
                    "metricName": {{ $slo.metric | quote }}
                  }
                ],
                "filter": {
                  "expression": {{ $slo.filter | quote }}
                },
                "groupBy": {{ $groupBy | toJson }},
                "order": [],
                "disabled": false
              }
            }
          ],
          "panelType": "graph",
          "queryType": "builder"
        },
        "selectedQueryName": "A",
        "op": {{ $op | quote }},
        "target": {{ $threshold }},
        "matchType": {{ $burnFast.matchType | default "3" | quote }},
        "targetUnit": "",
        "thresholds": {
          "kind": "basic",
          "spec": [
            {
              "name": {{ $severity | quote }},
              "target": {{ $threshold }},
              "targetUnit": "",
              "matchType": {{ $burnFast.matchType | default "3" | quote }},
              "op": {{ $op | quote }},
              "channels": {{ $channels | toJson }}
            }
          ]
        }
      },
      "preferredChannels": {{ $channels | toJson }}
    }
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ $slo.name }}-slo-budget-exhausted
  labels:
    app.kubernetes.io/managed-by: {{ .Release.Service }}
    app.kubernetes.io/instance: {{ .Release.Name }}
    helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
    signoz.io/alert: "true"
    signoz.io/alert-type: "slo-budget-exhausted"
  annotations:
    signoz.io/alert-name: {{ printf "%s SLO Budget Exhausted" $slo.name | quote }}
    signoz.io/severity: {{ $severity | quote }}
    signoz.io/notification-channels: {{ join "," $channels | quote }}
data:
  alert.json: |
    {
      "alert": {{ printf "%s SLO Budget Exhausted" $slo.name | quote }},
      "alertType": "METRIC_BASED_ALERT",
      "ruleType": "threshold_rule",
      "version": "v5",
      "broadcastToAll": false,
      "disabled": false,
      "evalWindow": {{ $budgetExhausted.evalWindow | default "6h0m0s" | quote }},
      "frequency": {{ $budgetExhausted.frequency | default "5m0s" | quote }},
      "severity": {{ $severity | quote }},
      "labels": {
        "service": {{ $slo.name | quote }},
        "alert_type": "slo_budget_exhausted",
        "environment": {{ $environment | quote }}
      },
      "annotations": {
        "summary": {{ printf "%s has exhausted its error budget" $slo.name | quote }},
        "description": "Error budget for the SLO window has been consumed. Service has been degraded for too long."
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
                    "timeAggregation": {{ $timeAgg | quote }},
                    "spaceAggregation": {{ $spaceAgg | quote }},
                    "metricName": {{ $slo.metric | quote }}
                  }
                ],
                "filter": {
                  "expression": {{ $slo.filter | quote }}
                },
                "groupBy": {{ $groupBy | toJson }},
                "order": [],
                "disabled": false
              }
            }
          ],
          "panelType": "graph",
          "queryType": "builder"
        },
        "selectedQueryName": "A",
        "op": {{ $op | quote }},
        "target": {{ $threshold }},
        "matchType": {{ $budgetExhausted.matchType | default "3" | quote }},
        "targetUnit": "",
        "thresholds": {
          "kind": "basic",
          "spec": [
            {
              "name": {{ $severity | quote }},
              "target": {{ $threshold }},
              "targetUnit": "",
              "matchType": {{ $budgetExhausted.matchType | default "3" | quote }},
              "op": {{ $op | quote }},
              "channels": {{ $channels | toJson }}
            }
          ]
        }
      },
      "preferredChannels": {{ $channels | toJson }}
    }
{{- end -}}
