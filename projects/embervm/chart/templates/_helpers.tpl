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

{{/*
The node daemon (embervm-noded) is a SECOND Deployment in this one chart/release.
It uses a DISTINCT app.kubernetes.io/name ("<name>-noded") so its selector is
disjoint from the control plane's: the control-plane Deployment's selector is
already live and immutable, so it cannot gain a component label without a
delete+recreate. The noded selector additionally carries a component label to
disambiguate self-documentingly (and satisfy the selector-component lint).
*/}}
{{- define "embervm.noded.name" -}}
{{- printf "%s-noded" (include "embervm.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "embervm.noded.fullname" -}}
{{- printf "%s-noded" (include "embervm.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "embervm.noded.selectorLabels" -}}
app.kubernetes.io/name: {{ include "embervm.noded.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: noded
{{- end -}}

{{- define "embervm.noded.labels" -}}
{{ include "embervm.noded.selectorLabels" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "embervm.noded.serviceAccountName" -}}
{{- include "embervm.noded.fullname" . -}}
{{- end -}}
