{{- define "scotland-connector.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "scotland-connector.selectorLabels" -}}
app.kubernetes.io/name: scotland-connector
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "scotland-connector.labels" -}}
{{ include "scotland-connector.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end -}}

{{/* Deploy by digest when the build stamped one, never by a tag that a
     content-identical push may have skipped. */}}
{{- define "scotland-connector.image" -}}
{{- if .Values.image.digest -}}
{{ .Values.image.repository }}@{{ .Values.image.digest }}
{{- else -}}
{{ .Values.image.repository }}:{{ .Values.image.tag }}
{{- end -}}
{{- end -}}
