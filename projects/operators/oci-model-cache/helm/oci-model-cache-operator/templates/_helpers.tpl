{{- define "oci-model-cache-operator.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "oci-model-cache-operator.fullname" -}}
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

{{- define "oci-model-cache-operator.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "oci-model-cache-operator.labels" -}}
helm.sh/chart: {{ include "oci-model-cache-operator.chart" . }}
{{ include "oci-model-cache-operator.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "oci-model-cache-operator.selectorLabels" -}}
app.kubernetes.io/name: {{ include "oci-model-cache-operator.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "oci-model-cache-operator.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (printf "%s-controller-manager" (include "oci-model-cache-operator.fullname" .)) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{- define "oci-model-cache-operator.hfTokenSecretName" -}}
{{- if .Values.hfToken.existingSecret }}
{{- .Values.hfToken.existingSecret }}
{{- else }}
{{- printf "%s-hf-token" (include "oci-model-cache-operator.fullname" .) }}
{{- end }}
{{- end }}

{{- define "oci-model-cache-operator.syncServiceAccountName" -}}
{{- if .Values.syncServiceAccount.create }}
{{- default (printf "%s-sync" (include "oci-model-cache-operator.fullname" .)) .Values.syncServiceAccount.name }}
{{- else }}
{{- default "default" .Values.syncServiceAccount.name }}
{{- end }}
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
{{- define "oci-model-cache-operator.imageRef" -}}
{{- if .Values.controllerManager.image.digest -}}
{{ .Values.controllerManager.image.repository }}@{{ .Values.controllerManager.image.digest }}
{{- else -}}
{{ .Values.controllerManager.image.repository }}:{{ .Values.controllerManager.image.tag | default .Chart.AppVersion }}
{{- end -}}
{{- end }}
