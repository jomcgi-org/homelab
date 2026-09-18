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
Name of the LLM Service.

BENCH MODE RENAMES THIS ON PURPOSE, and the rename IS the mechanism.

Every in-cluster caller reaches the LLM at the hardcoded DNS name
`inference.inference.svc.cluster.local` (monolith chart/values.yaml
LLAMA_CPP_URL in six places, monolith-dev, and the EmberVM egress allowlist).
Renaming the Service makes that name NXDOMAIN, so those callers fail at DNS
resolution, immediately and loudly, instead of hanging on a connect. The
external route keeps working because httproute-private.yaml resolves its
backendRef through this same helper, so both move together.

WHY NOT fullnameOverride, which would be the obvious lever: the Deployment
(deployment.yaml) derives its name from inference.fullname too. A
rename there deletes and recreates the Deployment, and on a single GPU the new
pod sits Pending on nvidia.com/gpu: 1 until ArgoCD prunes the old one. Renaming
only the Service leaves the Deployment, and therefore the GPU claim, untouched.

WHY NOT a NetworkPolicy, the other obvious lever: kubelet probes and the
Prometheus scrape both address the POD IP rather than the Service, so they are
unaffected by this and would have been at risk under a deny-by-default ingress
rule. There is no probe-bearing pod under such a policy anywhere in this
cluster to copy from, and the failure mode is a crash loop on a Recreate
deployment whose previous engine is already torn down.

The embeddings Service is deliberately NOT renamed. It is a separate CPU-only
deployment (nGpuLayers 0) that does not contend for the GPU, and cutting it
would break grimoire and knowledge retrieval for no benchmark benefit.
*/}}
{{- define "inference.serviceName" -}}
{{- if .Values.benchMode.enabled -}}
{{- printf "%s-bench" (include "inference.fullname" .) -}}
{{- else -}}
{{- include "inference.fullname" . -}}
{{- end -}}
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

{{/*
ninfer-serve CLI arguments. The artifact path is auto-discovered in the
container and passed as the positional argument, so it is not emitted here.
*/}}
{{- define "inference.ninferArgs" -}}
--host {{ .Values.server.host | quote }} \
--port {{ .Values.server.port | quote }} \
--model-id {{ .Values.ninfer.modelId | quote }} \
--max-context {{ .Values.ninfer.maxContext | quote }} \
--kv-capacity {{ .Values.ninfer.kvCapacity | quote }} \
--kv-dtype {{ .Values.ninfer.kvDtype | quote }} \
--max-concurrency {{ .Values.ninfer.maxConcurrency | quote }} \
--max-pending-requests {{ .Values.ninfer.maxPendingRequests | quote }} \
--pending-timeout-ms {{ .Values.ninfer.pendingTimeoutMs | quote }} \
--prefill-chunk {{ .Values.ninfer.prefillChunk | quote }} \
{{- if .Values.ninfer.spec }}
--spec {{ .Values.ninfer.spec | quote }} \
--draft-tokens {{ .Values.ninfer.draftTokens | quote }} \
{{- end }}
{{- if .Values.ninfer.lmHeadDraft }}
--lm-head-draft \
{{- end }}
{{- if .Values.ninfer.vision }}
--vision \
{{- end }}
{{- if .Values.ninfer.preserveThinking }}
--preserve-thinking \
{{- end }}
--log-stats-interval-ms "10000"{{ range .Values.ninfer.extraArgs }} \
{{ . | quote }}{{ end }}
{{- end }}
