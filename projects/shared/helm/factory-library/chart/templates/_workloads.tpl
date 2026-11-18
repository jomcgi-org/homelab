{{- define "factory-library.pod" -}}
{{- $w := .workload -}}
{{- include "factory-library.name" ($w.containerName | default .name) -}}
{{- if and (hasKey $w "kubernetesApi") (not (kindIs "bool" $w.kubernetesApi)) -}}{{- fail "factory-library: kubernetesApi must be a boolean" -}}{{- end -}}
{{- range $field := list "replicas" "backoffLimit" -}}
{{- if and (hasKey $w $field) (not (regexMatch "^[0-9]+$" (toString (index $w $field)))) -}}
{{- fail (printf "factory-library: %s must be a nonnegative integer" $field) -}}
{{- end -}}
{{- end -}}
{{- if and $w.concurrencyPolicy (not (has $w.concurrencyPolicy (list "Allow" "Forbid" "Replace"))) -}}{{- fail "factory-library: invalid concurrencyPolicy" -}}{{- end -}}
{{- if and (hasKey $w "hook") (ne ($w.kind | default "Deployment") "Job") -}}{{- fail (printf "factory-library: workload %s: hook is only for Jobs" .name) -}}{{- end -}}
{{- if and (hasKey $w "hook") (not (has $w.hook (list "Sync" "PostSync"))) -}}{{- fail (printf "factory-library: workload %s: hook must be Sync or PostSync" .name) -}}{{- end -}}
{{- if hasKey $w "hookWave" -}}
{{- if not $w.hook -}}{{- fail "factory-library: hookWave requires a Job hook" -}}{{- end -}}
{{- if not (regexMatch "^-?[0-9]+$" (toString $w.hookWave)) -}}{{- fail "factory-library: hookWave must be an integer" -}}{{- end -}}
{{- end -}}
{{- include "factory-library.keys" (dict "path" (printf "workload %s" .name) "value" $w "allowed" (list "kind" "image" "containerName" "replicas" "schedule" "concurrencyPolicy" "backoffLimit" "hook" "hookWave" "kubernetesApi" "resources" "command" "args" "env" "ports" "readinessProbe" "livenessProbe" "startupProbe" "writablePaths" "service" "network")) -}}
{{- $resources := $w.resources | default dict -}}
{{- $requests := $resources.requests | default dict -}}
{{- $limits := $resources.limits | default dict -}}
{{- $_ := required (printf "factory-library: workload %s: resources.requests.cpu is required" .name) $requests.cpu -}}
{{- $_ := required (printf "factory-library: workload %s: resources.requests.memory is required" .name) $requests.memory -}}
{{- $_ := required (printf "factory-library: workload %s: resources.limits.memory is required" .name) $limits.memory -}}
{{- $image := include "factory-library.image" . -}}
metadata:
  labels:
    {{- include "factory-library.selector" . | nindent 4 }}
    {{- if and .root.Values.buckets (not .secretInitializer) }}
    factory.jomcgi.dev/garage-client: "true"
    {{- end }}
  {{- include "factory-library.annotations" . | nindent 2 }}
spec:
  automountServiceAccountToken: {{ $w.kubernetesApi | default false }}
  {{- if or $w.kubernetesApi .mirror }}
  serviceAccountName: {{ .name }}
  {{- end }}
  {{- if hasPrefix "ghcr.io/jomcgi/" $image }}
  imagePullSecrets:
    - name: ghcr-imagepull-secret
  {{- end }}
  securityContext:
    runAsNonRoot: true
    runAsUser: {{ include "factory-library.securityID" (dict "root" .root "name" .name "default" "runAsUser") }}
    runAsGroup: {{ include "factory-library.securityID" (dict "root" .root "name" .name "default" "runAsGroup") }}
    seccompProfile:
      type: RuntimeDefault
  {{- if ne ($w.kind | default "Deployment") "Deployment" }}
  restartPolicy: Never
  {{- end }}
  containers:
    - name: {{ $w.containerName | default .name }}
      image: {{ $image | quote }}
      {{- range $field := list "command" "args" "env" "ports" "readinessProbe" "livenessProbe" "startupProbe" }}
      {{- with index $w $field }}
      {{ $field }}:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- end }}
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: {{ not (include "factory-library.exception" (dict "root" .root "name" .name "default" "readOnlyRootFilesystem")) }}
        capabilities:
          drop: [ALL]
      resources:
        {{- toYaml $resources | nindent 8 }}
      {{- with $w.writablePaths }}
      volumeMounts:
        {{- range . }}
        {{- include "factory-library.keys" (dict "path" "writablePaths" "value" . "allowed" (list "name" "mountPath" "sizeLimit" "medium")) }}
        {{- include "factory-library.name" (required "factory-library: writablePaths.name is required" .name) }}
        - name: {{ required "factory-library: writablePaths.name is required" .name }}
          mountPath: {{ required "factory-library: writablePaths.mountPath is required" .mountPath | quote }}
        {{- end }}
      {{- end }}
  {{- with $w.writablePaths }}
  volumes:
    {{- range . }}
    - name: {{ .name }}
      emptyDir:
        {{- toYaml (pick . "sizeLimit" "medium") | nindent 8 }}
    {{- end }}
  {{- end }}
{{- end -}}

{{- define "factory-library.deployment" -}}
{{- include "factory-library.validateExceptions" .root }}
{{- $_ := set . "workload" (mergeOverwrite (deepCopy .workload) (dict "kind" "Deployment")) }}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ .name }}
  {{- include "factory-library.annotations" . | nindent 2 }}
spec:
  replicas: {{ dig "replicas" 1 .workload }}
  selector:
    matchLabels:
      {{- include "factory-library.selector" . | nindent 6 }}
  template:
    {{- include "factory-library.pod" . | nindent 4 }}
{{ include "factory-library.companions" . }}
{{- end -}}

{{- define "factory-library.job" -}}
{{- include "factory-library.validateExceptions" .root }}
{{- $_ := set . "workload" (mergeOverwrite (deepCopy .workload) (dict "kind" "Job")) }}
---
apiVersion: batch/v1
kind: Job
metadata:
  name: {{ .name }}
  {{- if .secretInitializer }}
  annotations:
    argocd.argoproj.io/hook: Sync
    argocd.argoproj.io/hook-delete-policy: BeforeHookCreation,HookSucceeded
    argocd.argoproj.io/sync-wave: "-1"
  {{- else if .workload.hook }}
  {{- /* An Argo CD hook runs on every sync, so a Job whose spec changes with
  each release (a deploy or a post-deploy check) never hits Job immutability. */}}
  annotations:
    argocd.argoproj.io/hook: {{ .workload.hook }}
    argocd.argoproj.io/hook-delete-policy: BeforeHookCreation,HookSucceeded
    {{- if hasKey .workload "hookWave" }}
    argocd.argoproj.io/sync-wave: {{ .workload.hookWave | quote }}
    {{- end }}
    {{- $exceptions := list }}
    {{- range .root.Values.exceptions }}
    {{- if eq .workload $.name }}
    {{- $exceptions = append $exceptions . }}
    {{- end }}
    {{- end }}
    {{- if $exceptions }}
    factory.jomcgi.dev/exceptions: {{ $exceptions | toJson | quote }}
    {{- end }}
  {{- else }}
  {{- include "factory-library.annotations" . | nindent 2 }}
  {{- end }}
spec:
  backoffLimit: {{ dig "backoffLimit" 3 .workload }}
  template:
    {{- include "factory-library.pod" . | nindent 4 }}
{{ include "factory-library.companions" . }}
{{- end -}}

{{- define "factory-library.cronjob" -}}
{{- include "factory-library.validateExceptions" .root }}
{{- $_ := set . "workload" (mergeOverwrite (deepCopy .workload) (dict "kind" "CronJob")) }}
---
apiVersion: batch/v1
kind: CronJob
metadata:
  name: {{ .name }}
  {{- include "factory-library.annotations" . | nindent 2 }}
spec:
  schedule: {{ required "factory-library: CronJob.schedule is required" .workload.schedule | quote }}
  concurrencyPolicy: {{ .workload.concurrencyPolicy | default "Forbid" }}
  {{- if .mirror }}
  suspend: {{ .suspend | default false }}
  {{- end }}
  jobTemplate:
    {{- with include "factory-library.annotations" . }}
    metadata:
      {{- . | nindent 6 }}
    {{- end }}
    spec:
      backoffLimit: {{ dig "backoffLimit" 3 .workload }}
      template:
        {{- include "factory-library.pod" . | nindent 8 }}
{{ include "factory-library.companions" . }}
{{- end -}}

{{- define "factory-library.companions" -}}
{{- if or .workload.kubernetesApi .mirror }}
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: {{ .name }}
  {{- if .secretInitializer }}
  annotations:
    argocd.argoproj.io/sync-wave: "-2"
  {{- else }}
  {{- include "factory-library.annotations" . | nindent 2 }}
  {{- end }}
automountServiceAccountToken: false
{{- end }}
{{ include "factory-library.networkPolicy" . }}
{{- if .workload.service }}
{{ include "factory-library.service" . }}
{{- end }}
{{- end -}}

{{- define "factory-library.service" -}}
{{- include "factory-library.validateExceptions" .root }}
{{- include "factory-library.keys" (dict "path" "service" "value" .workload.service "allowed" (list "ports")) }}
---
apiVersion: v1
kind: Service
metadata:
  name: {{ .name }}
  {{- include "factory-library.annotations" . | nindent 2 }}
spec:
  selector:
    {{- include "factory-library.selector" . | nindent 4 }}
  ports:
    {{- toYaml (required "factory-library: service.ports is required" .workload.service.ports) | nindent 4 }}
{{- end -}}
