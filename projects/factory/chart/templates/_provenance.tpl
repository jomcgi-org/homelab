{{/* One rendering for the Agent and its dispatch provenance. */}}
{{- define "factory.bindingPrompt" -}}
{{- $r := .binding -}}
{{- $prompt := (index .root.Values.agents $r.agent).systemPrompt -}}
{{- if eq $r.agent "planner" -}}
{{- range .root.Values.connector.plannerSchedules -}}
{{- if .actions -}}
{{- $prompt = printf "%s\nFor the reviewed %s task, return its requested triage JSON object instead of the proposal array." $prompt .name -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if eq ($r.toolchain | default "") "homelab" -}}
{{- $prompt = printf "%s\n%s" $prompt (.root.Files.Get "files/toolchain-prompt.txt") -}}
{{- end -}}
{{- if $r.memory -}}
{{- $prompt = printf "%s\n%s" $prompt (.root.Files.Get "files/memory-prompt.txt") -}}
{{- end -}}
{{- $prompt -}}
{{- end -}}

{{- define "factory.runRuntime" -}}
{{- $a := index .root.Values.agents .binding.agent -}}
{{- $effort := required "run provenance requires broker.config.codex.reasoningEffort" ((.root.Values.broker.config.codex | default dict).reasoningEffort) -}}
{{- dict "model" $a.model "reasoningEffort" $effort "arm" "control" "systemPrompt" (include "factory.bindingPrompt" .) | toJson -}}
{{- end -}}
