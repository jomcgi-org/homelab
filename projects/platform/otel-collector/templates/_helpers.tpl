{{/*
Expand the name of the chart.
*/}}
{{- define "otel-collector.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create the resource name.
*/}}
{{- define "otel-collector.fullname" -}}
{{- if contains .Chart.Name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{/*
Create the chart name and version label.
*/}}
{{- define "otel-collector.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Selector labels.
*/}}
{{- define "otel-collector.selectorLabels" -}}
app.kubernetes.io/name: {{ include "otel-collector.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Common labels.
*/}}
{{- define "otel-collector.labels" -}}
helm.sh/chart: {{ include "otel-collector.chart" . }}
{{ include "otel-collector.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Service account name.
*/}}
{{- define "otel-collector.serviceAccountName" -}}
{{- include "otel-collector.fullname" . }}
{{- end }}

{{/*
Log agent (DaemonSet) naming and labels.

The selector labels deliberately use a DIFFERENT app.kubernetes.io/name from
the gateway. The gateway Service selects on name + instance only, so if the
agent pods shared the gateway's name label the Service would load-balance
OTLP traffic onto agent pods that do not listen on 4317/4318. The gateway
Deployment's selector is immutable, so the agent is the side that differs.
*/}}
{{- define "otel-collector.logs.fullname" -}}
{{- printf "%s-logs" (include "otel-collector.fullname" . | trunc 58 | trimSuffix "-") }}
{{- end }}

{{- define "otel-collector.logs.selectorLabels" -}}
app.kubernetes.io/name: {{ include "otel-collector.name" . | trunc 58 | trimSuffix "-" }}-logs
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: logs
{{- end }}

{{- define "otel-collector.logs.labels" -}}
helm.sh/chart: {{ include "otel-collector.chart" . }}
{{ include "otel-collector.logs.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Cluster agent (single-replica Deployment) naming and labels. Its name label
differs from the gateway's for the same reason as the log agent's.
*/}}
{{- define "otel-collector.cluster.fullname" -}}
{{- printf "%s-cluster" (include "otel-collector.fullname" . | trunc 55 | trimSuffix "-") }}
{{- end }}

{{- define "otel-collector.cluster.selectorLabels" -}}
app.kubernetes.io/name: {{ include "otel-collector.name" . | trunc 55 | trimSuffix "-" }}-cluster
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: cluster
{{- end }}

{{- define "otel-collector.cluster.labels" -}}
helm.sh/chart: {{ include "otel-collector.chart" . }}
{{ include "otel-collector.cluster.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
A string as the body of a double-quoted OTTL literal inside a YAML
single-quoted scalar: backslashes and double quotes escaped for OTTL, $ doubled
so the collector does not expand ${...}, single quotes doubled for YAML.
*/}}
{{- define "otel-collector.ottlString" -}}
{{- . | replace "\\" "\\\\" | replace "\"" "\\\"" | replace "$" "$$" | replace "'" "''" -}}
{{- end }}
