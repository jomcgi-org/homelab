{{/* Common labels for resources this chart owns (not the kagent subchart's). */}}
{{- define "factory.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: factory
{{- end }}

{{/* The namespace kagent is installed in. */}}
{{- define "factory.kagentNamespace" -}}
{{- .Values.kagent.namespaceOverride | default .Release.Namespace -}}
{{- end }}

{{/* "true" when this chart installs kagent. */}}
{{- define "factory.kagentInstall" -}}
{{- if eq .Values.kagent.mode "install" -}}true{{- end -}}
{{- end }}

{{/* Selector labels of the kagent subchart's pods, by component. */}}
{{- define "factory.kagentSelector" -}}
app.kubernetes.io/name: kagent
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{/* The CRD names in crds/, from their file names (api.kagent.dev_<plural>.yaml). */}}
{{- define "factory.kagentCRDNames" -}}
{{- $names := list -}}
{{- range $path, $_ := .Files.Glob "crds/api.kagent.dev_*.yaml" -}}
{{- $names = append $names (printf "%s.api.kagent.dev" (base $path | trimPrefix "api.kagent.dev_" | trimSuffix ".yaml")) -}}
{{- end -}}
{{- $names | toJson -}}
{{- end }}
