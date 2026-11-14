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

{{/* The credential broker's name: its ServiceAccount, Deployment and Service. */}}
{{- define "factory.brokerName" -}}factory-broker{{- end }}

{{/* Selector labels of the broker's pods. */}}
{{- define "factory.brokerSelector" -}}
app.kubernetes.io/name: factory-broker
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/* The broker image, by digest once helm_chart(images) has pinned it. */}}
{{- define "factory.brokerImage" -}}
{{- $i := .Values.broker.image -}}
{{- if $i.digest -}}{{ $i.repository }}@{{ $i.digest }}{{- else -}}{{ $i.repository }}:{{ $i.tag }}{{- end -}}
{{- end }}

{{/*
The relay's base URL for ModelConfigs: a DNS name, cleartext through
Substrate's egress gateway. Fully qualified: Substrate resolves actor
destinations as absolute names, so a search-domain name such as
<svc>.<ns>.svc gets NXDOMAIN.
*/}}
{{- define "factory.brokerRelayURL" -}}
http://{{ include "factory.brokerName" . }}.{{ .Values.broker.namespace }}.svc.{{ .Values.clusterDomain }}/v1
{{- end }}

{{/* The broker's grant Secret and quota ConfigMap names. */}}
{{- define "factory.brokerGrantSecret" -}}factory-broker-grant-{{ .Values.broker.grant }}{{- end }}
{{- define "factory.brokerQuotaConfigMap" -}}factory-broker-quota{{- end }}

{{/* The kagent Agent (and its WorkerPool) of a repository binding: <name>-<agent>. */}}
{{- define "factory.bindingAgentName" -}}{{ .name }}-{{ .agent }}{{- end }}

{{/* Keep annotations for state that must outlive the Application. */}}
{{- define "factory.keep" -}}
argocd.argoproj.io/sync-options: Prune=false,Delete=false
helm.sh/resource-policy: keep
{{- end }}
