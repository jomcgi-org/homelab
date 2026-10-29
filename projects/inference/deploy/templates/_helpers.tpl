{{/*
Expand the name of the chart.
*/}}
{{- define "inference.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "inference.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "inference.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "inference.labels" -}}
helm.sh/chart: {{ include "inference.chart" . }}
{{ include "inference.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "inference.selectorLabels" -}}
app.kubernetes.io/name: {{ include "inference.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Embedding llama-server CLI arguments.
*/}}
{{- define "inference.embeddingArgs" -}}
--n-gpu-layers {{ .Values.embeddings.llamaCpp.nGpuLayers | quote }} \
--ctx-size {{ .Values.embeddings.llamaCpp.ctxSize | quote }} \
{{- if .Values.embeddings.llamaCpp.flashAttn }}
--flash-attn {{ .Values.embeddings.llamaCpp.flashAttn | quote }} \
{{- end }}
--cache-type-k {{ .Values.embeddings.llamaCpp.cacheTypeK | quote }} \
--cache-type-v {{ .Values.embeddings.llamaCpp.cacheTypeV | quote }} \
--threads {{ .Values.embeddings.llamaCpp.threads | quote }} \
{{- if .Values.embeddings.llamaCpp.jinja }}
--jinja \
{{- end }}
--host {{ .Values.server.host | quote }} \
--port {{ .Values.server.port | quote }}{{ range .Values.embeddings.llamaCpp.extraArgs }} \
{{ . | quote }}{{ end }}
{{- end }}
