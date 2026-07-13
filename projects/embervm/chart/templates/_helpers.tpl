{{- define "embervm.name" -}}
{{- default "embervm" .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "embervm.fullname" -}}
{{- printf "%s-%s" .Release.Name (include "embervm.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "embervm.labels" -}}
app.kubernetes.io/name: {{ include "embervm.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "embervm.selectorLabels" -}}
app.kubernetes.io/name: {{ include "embervm.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "embervm.serviceAccountName" -}}
{{- include "embervm.fullname" . -}}
{{- end -}}
