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
--ctx-size {{ .Values.llamacpp.ctxSize | quote }} \
--parallel {{ .Values.llamacpp.parallel | quote }} \
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
