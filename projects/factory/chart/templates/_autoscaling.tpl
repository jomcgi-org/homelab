{{- /*
Worker pool autoscaling (ARCHITECTURE.md, "Worker pool autoscaling"). Off by
default: a pool's replicas stay agents.<name>.concurrency and no object here
renders. With agents.<name>.autoscaling.enabled, each binding's WorkerPool
gets a HorizontalPodAutoscaler that scales it between minReplicas (0) and
maxReplicas on factory_worker_pool_workers_wanted, which Prometheus Adapter
serves (projects/platform-factory/prometheus-adapter).
*/ -}}

{{/* The autoscaling settings of an agent profile, defaults filled in, as JSON. */}}
{{- define "factory.workerAutoscaling" -}}
{{- $a := .agent -}}
{{- $as := $a.autoscaling | default dict -}}
{{- $spare := 2 -}}
{{- if hasKey $as "spare" }}{{ $spare = int $as.spare }}{{ end -}}
{{- $min := 0 -}}
{{- if hasKey $as "minReplicas" }}{{ $min = int $as.minReplicas }}{{ end -}}
{{- /* The reviewer is not a connector binding: it sends to kagent itself, so the connector publishes no pending demand for its pool and the reaper publishes pending 0 in its place (FACTORY_REAPER_UNBOUND_POOLS). The pool then grows only from the actors it hosts, which two floors cover. */ -}}
{{- if and (eq (toString $as.enabled) "true") (eq (.key | default "") "reviewer") -}}
{{- $burst := int .root.Values.reviewer.concurrency -}}
{{- if lt $min $burst -}}
{{- fail (printf "autoscaling: the reviewer pool needs minReplicas of at least reviewer.concurrency (%d), got %d: its sends reach kagent without the connector, so a burst of reviews cannot wait for a scale-up" $burst $min) -}}
{{- end -}}
{{- if lt $spare 1 -}}
{{- fail "autoscaling: the reviewer pool needs a spare of at least 1: it grows from the actors it hosts, and a goal check session needs a worker beside the review session's" -}}
{{- end -}}
{{- end -}}
{{- $max := add (int $a.concurrency) $spare -}}
{{- if $as.maxReplicas }}{{ $max = int $as.maxReplicas }}{{ end -}}
{{- $down := $as.scaleDown | default dict -}}
{{- $stabilization := 1800 -}}
{{- if hasKey $down "stabilizationSeconds" }}{{ $stabilization = int $down.stabilizationSeconds }}{{ end -}}
{{- $period := 300 -}}
{{- if hasKey $down "periodSeconds" }}{{ $period = int $down.periodSeconds }}{{ end -}}
{{- if gt $min $max -}}
{{- fail (printf "autoscaling: minReplicas %d is above maxReplicas %d" $min $max) -}}
{{- end -}}
{{- dict "enabled" (ternary true false (eq (toString $as.enabled) "true")) "spare" $spare "minReplicas" $min "maxReplicas" $max "stabilizationSeconds" $stabilization "periodSeconds" $period | toJson -}}
{{- end -}}

{{/*
The HorizontalPodAutoscaler of one WorkerPool, or nothing. It emits a leading
"---" document separator, so the caller includes it between documents and an
off pool renders byte for byte as before.
*/}}
{{- define "factory.workerPoolAutoscaler" -}}
{{- $s := include "factory.workerAutoscaling" (dict "agent" .agent "key" .key "root" .root) | fromJson -}}
{{- if $s.enabled -}}
{{ "\n" }}---
# Scales the WorkerPool of the same name on the workers it needs. The HPA owns
# spec.replicas of the pool, and the pool's spec.replicas in this chart is only
# its first value. Argo CD must ignore that field for this pool
# (factory/deploy/application.yaml ignoreDifferences), and a pool at zero was
# put there by this HPA, which records ScaledToZero.
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: {{ .pool }}
  namespace: {{ .namespace }}
  labels:
    {{- include "factory.labels" .root | nindent 4 }}
  annotations:
    argocd.argoproj.io/sync-wave: "3"
spec:
  scaleTargetRef:
    apiVersion: ate.dev/v1
    kind: WorkerPool
    name: {{ .pool }}
  minReplicas: {{ $s.minReplicas }}
  maxReplicas: {{ $s.maxReplicas }}
  metrics:
    - type: External
      external:
        metric:
          name: factory_worker_pool_workers_wanted
          selector:
            matchLabels:
              pool_namespace: {{ .namespace | quote }}
              pool: {{ .pool | quote }}
        target:
          type: AverageValue
          averageValue: "1"
  behavior:
    # tolerance 0: the HPA's default 10% would ignore a gap below 10% of the
    # current replicas, which with AverageValue 1 hides one worker above about
    # 10 replicas. Needs the HPAConfigurableTolerance feature gate; the API
    # drops the field when it is off.
    # A run that arrives at zero wakes the whole pool at once.
    scaleUp:
      tolerance: "0"
      stabilizationWindowSeconds: 0
      policies:
        - type: Pods
          value: {{ $s.maxReplicas }}
          periodSeconds: 15
    # Slow and one pod at a time: the pool follows the highest demand of the
    # last stabilizationSeconds, and the reaper marks the workers that host an
    # actor so the ReplicaSet removes idle ones first.
    scaleDown:
      tolerance: "0"
      stabilizationWindowSeconds: {{ $s.stabilizationSeconds }}
      policies:
        - type: Pods
          value: 1
          periodSeconds: {{ $s.periodSeconds }}
{{- end -}}
{{- end -}}

{{/*
The pools whose autoscaling is on, as JSON: [{namespace, name, spare,
unbound}]. The reaper publishes demand and capacity for exactly these.
unbound marks a pool no connector binding sends to (the reviewer's): the
reaper also publishes its pending demand, at 0.
*/}}
{{- define "factory.autoscaledPools" -}}
{{- $ := . -}}
{{- $out := list -}}
{{- range $r := .Values.repositories -}}
{{- $coder := include "factory.workerAutoscaling" (dict "agent" (index $.Values.agents $r.agent) "key" $r.agent "root" $) | fromJson -}}
{{- if $coder.enabled -}}
{{- $out = append $out (dict "namespace" $r.namespace "name" (include "factory.bindingAgentName" $r) "spare" $coder.spare "unbound" false) -}}
{{- end -}}
{{- if $.Values.reviewer.enabled -}}
{{- $review := include "factory.workerAutoscaling" (dict "agent" (index $.Values.agents "reviewer") "key" "reviewer" "root" $) | fromJson -}}
{{- if $review.enabled -}}
{{- $out = append $out (dict "namespace" (include "factory.reviewerNamespace" (dict "root" $ "binding" $r)) "name" (include "factory.bindingAgentName" (dict "name" $r.name "agent" "reviewer")) "spare" $review.spare "unbound" true) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- toJson $out -}}
{{- end -}}
