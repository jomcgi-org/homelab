{{- define "git-mirror.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "git-mirror.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "git-mirror.labels" -}}
app.kubernetes.io/name: {{ include "git-mirror.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/component: mirror
{{- end -}}

{{- define "git-mirror.selectorLabels" -}}
app.kubernetes.io/name: {{ include "git-mirror.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
