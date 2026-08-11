{{/*
Expand the name of the chart.
*/}}
{{- define "signoz-dashboard-sidecar.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "signoz-dashboard-sidecar.fullname" -}}
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
{{- define "signoz-dashboard-sidecar.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "signoz-dashboard-sidecar.labels" -}}
helm.sh/chart: {{ include "signoz-dashboard-sidecar.chart" . }}
{{ include "signoz-dashboard-sidecar.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "signoz-dashboard-sidecar.selectorLabels" -}}
app.kubernetes.io/name: {{ include "signoz-dashboard-sidecar.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "signoz-dashboard-sidecar.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "signoz-dashboard-sidecar.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Get the image tag
*/}}
{{- define "signoz-dashboard-sidecar.imageTag" -}}
{{- .Values.image.tag | default .Chart.AppVersion }}
{{- end }}

{{/*
Container image reference. Prefers the content-addressed digest, falling back to
the tag when no digest is set.

WHY THE DIGEST WINS. helm_images_values emits repository, tag AND digest, and
the tag is build-timestamped: it moves on every commit to main even when the
image bytes are identical. push-changed.sh skips pushing an image whose content
digest is already in the registry, so that new tag is frequently never created,
and deploying it is an ImagePullBackOff. That wedged monolith-public on
2026-08-11 (PR #4680 fixed the shared homelab-library the same way; this chart
does not use that library, so it needed its own).

The tag fallback keeps the chart renderable when no digest is present, e.g. a
plain `helm template` from source values.
*/}}
{{- define "signoz-dashboard-sidecar.imageRef" -}}
{{- if .Values.image.digest -}}
{{ .Values.image.repository }}@{{ .Values.image.digest }}
{{- else -}}
{{ .Values.image.repository }}:{{ include "signoz-dashboard-sidecar.imageTag" . }}
{{- end -}}
{{- end }}
