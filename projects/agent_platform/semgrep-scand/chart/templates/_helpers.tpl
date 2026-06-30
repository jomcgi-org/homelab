{{- define "semgrep-scand.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "semgrep-scand.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "semgrep-scand.labels" -}}
app.kubernetes.io/name: {{ include "semgrep-scand.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/component: scanner
{{- end -}}

{{- define "semgrep-scand.selectorLabels" -}}
app.kubernetes.io/name: {{ include "semgrep-scand.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "semgrep-scand.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "semgrep-scand.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}
