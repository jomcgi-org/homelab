{{/*
Expand the name of the chart.
*/}}
{{- define "homelab.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "homelab.fullname" -}}
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
{{- define "homelab.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "homelab.labels" -}}
helm.sh/chart: {{ include "homelab.chart" . }}
{{ include "homelab.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- with .Values.extraLabels }}
{{ toYaml . }}
{{- end }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "homelab.selectorLabels" -}}
app.kubernetes.io/name: {{ include "homelab.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "homelab.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "homelab.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Component labels — adds app.kubernetes.io/component to common labels.
Usage: {{ include "homelab.componentLabels" (dict "context" . "component" "api") }}
*/}}
{{- define "homelab.componentLabels" -}}
{{ include "homelab.labels" .context }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{/*
Component selector labels — adds app.kubernetes.io/component to selector labels.
Usage: {{ include "homelab.componentSelectorLabels" (dict "context" . "component" "api") }}
*/}}
{{- define "homelab.componentSelectorLabels" -}}
{{ include "homelab.selectorLabels" .context }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{/*
Container image reference. Prefers the content-addressed digest, falling back to
the tag when no digest is set.
Usage: {{ include "homelab.imageRef" .Values.web.image }}

WHY THE DIGEST WINS. helm_images_values emits repository, tag AND digest for
every Bazel-built image, and the tag is build-timestamped: it moves on every
commit to main even when the image bytes are identical. bazel/images/push/
push-changed.sh skips pushing an image whose content digest is already in the
registry, so that new tag is frequently never created. A chart that deployed
`repository:tag` therefore pinned a tag that does not exist, which is an
ImagePullBackOff. That took monolith-public's rollout down on 2026-08-11 (chart
0.287.0, commit 95eb93de7, where both public images were content-identical and
neither push ran). The digest is what push-changed.sh proves is published, so
it is the only ref that is correct by construction.

It also stops the churn: an unchanged image renders an identical Deployment, so
a chart republish no longer rolls the pods. The hand-written monolith and
embervm templates have always done this; the library is catching up.

The tag fallback is load-bearing for upstream images that never go through
helm_images_values and so carry no digest key (imgproxy pins its own digest
inside the tag string).
*/}}
{{- define "homelab.imageRef" -}}
{{- if .digest -}}
{{ .repository }}@{{ .digest }}
{{- else -}}
{{ .repository }}:{{ .tag }}
{{- end -}}
{{- end }}
