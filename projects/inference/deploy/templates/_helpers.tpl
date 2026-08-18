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
(deployment-llamacpp.yaml) derives its name from inference.fullname too. A
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
Concurrent llama.cpp slots. ctxSize is the TOTAL KV pool divided across these,
so lowering the count widens every remaining slot at identical VRAM: bench mode
gives its single slot the whole 98304-token pool instead of 32768.

One slot also removes batch interference from the timings, which is only
tolerable because the Service rename above has already cut the in-cluster
callers off. At 3 slots they would interleave and skew results; at 1 slot with
callers still connected they would queue behind a full generation, up to about
5.7 minutes at max_tokens 16384 and the observed 48 t/s.
*/}}
{{- define "inference.llamaCppParallel" -}}
{{- if .Values.benchMode.enabled -}}1{{- else -}}{{ .Values.llamacpp.parallel }}{{- end -}}
{{- end }}

{{/*
Total KV pool in tokens. Bench mode overrides it here rather than by editing
llamacpp.ctxSize, so the single benchMode flag still restores everything: an
override left behind in production would run 3 slots against a pool sized for
one, quietly tightening VRAM below the headroom that value was chosen for.

Sizing is solved from three measured points rather than estimated. See the
llamacpp.ctxSize comment in values.yaml for the derivation and for why the
GiB-per-32k rule of thumb it replaced was 35% too pessimistic.
*/}}
{{- define "inference.llamaCppCtxSize" -}}
{{- if .Values.benchMode.enabled -}}{{ .Values.benchMode.ctxSize }}{{- else -}}{{ .Values.llamacpp.ctxSize }}{{- end -}}
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
LLM llama-server CLI arguments (llm.engine "llamacpp").

Mirrors inference.embeddingArgs but for the generative path: GPU offload, a
multi-slot KV pool, and the jinja/reasoning flags that make tool calls and
reasoning_content come back in the shape the monolith already parses. The model
and mmproj paths are auto-discovered in the container, so they are not emitted
here.
*/}}
{{- define "inference.llamaCppArgs" -}}
--n-gpu-layers {{ .Values.llamacpp.nGpuLayers | quote }} \
--ctx-size {{ include "inference.llamaCppCtxSize" . | quote }} \
--parallel {{ include "inference.llamaCppParallel" . | quote }} \
{{- if .Values.llamacpp.flashAttn }}
--flash-attn {{ .Values.llamacpp.flashAttn | quote }} \
{{- end }}
--cache-type-k {{ .Values.llamacpp.cacheTypeK | quote }} \
--cache-type-v {{ .Values.llamacpp.cacheTypeV | quote }} \
--threads {{ .Values.llamacpp.threads | quote }} \
--host {{ .Values.server.host | quote }} \
--port {{ .Values.server.port | quote }}{{ range .Values.llamacpp.extraArgs }} \
{{ . | quote }}{{ end }}
{{- end }}

{{/*
vLLM CLI arguments.
*/}}
{{- define "inference.vllmArgs" -}}
--host {{ .Values.server.host }} \
--port {{ .Values.server.port }} \
--max-model-len {{ .Values.vllm.maxModelLen }} \
--gpu-memory-utilization {{ .Values.vllm.gpuMemoryUtilization }} \
{{- if .Values.vllm.quantization }}
--quantization {{ .Values.vllm.quantization }} \
{{- end }}
{{- if .Values.vllm.tokenizer }}
--tokenizer {{ .Values.vllm.tokenizer }} \
{{- end }}
{{- if .Values.server.chatTemplate }}
--chat-template /etc/chat-template/chat-template.jinja \
{{- end }}
{{- range .Values.vllm.extraArgs }}
{{ . | quote }} \
{{- end }}
--dtype {{ .Values.vllm.dtype }}
{{- end }}
