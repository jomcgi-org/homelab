{{/*
Helpers for the scheduled planner (HOME-40). Nothing renders unless
planner.enabled; see values.yaml "planner".
*/}}

{{/* The itsaplan agent whose runs the connector claims for planning. */}}
{{- define "factory.plannerAgentKey" -}}homelab-planner{{- end }}

{{/* The prefix of each itsaplan schedule's name and prompt. */}}
{{- define "factory.plannerSchedulePrefix" -}}planner-{{- end }}
{{- define "factory.plannerPromptPrefix" -}}factory-planner-schedule:{{- end }}

{{/* The repositories entry the planner reads, as JSON; fails when it is missing. */}}
{{- define "factory.plannerRepository" -}}
{{- $found := dict -}}
{{- range .Values.repositories -}}
{{- if eq .name $.Values.planner.repository -}}{{- $found = . -}}{{- end -}}
{{- end -}}
{{- if not $found -}}
{{- fail (printf "planner.repository %q names no repository in repositories" .Values.planner.repository) -}}
{{- end -}}
{{- toJson $found -}}
{{- end }}

{{/* The planner Agent's own namespace. */}}
{{- define "factory.plannerNamespace" -}}
{{- $r := include "factory.plannerRepository" . | fromJson -}}
{{- .Values.planner.namespace | default (printf "%s-planner" $r.namespace) -}}
{{- end }}

{{/* The kagent Agent's name, as factory.bindingResources derives it. */}}
{{- define "factory.plannerAgentName" -}}
{{- $r := include "factory.plannerRepository" . | fromJson -}}
{{- include "factory.bindingAgentName" (dict "name" $r.name "agent" "planner") -}}
{{- end }}

{{/*
The jobs that get an itsaplan cron schedule, as a JSON list of {name, cron,
prompt}. An entry needs both cron and prompt, or neither (it never runs). The
prompt names the job for the connector and is never sent to a model.
*/}}
{{- define "factory.plannerBootstrapSchedules" -}}
{{- $out := list -}}
{{- if .Values.planner.enabled -}}
{{- range .Values.connector.plannerSchedules | default list -}}
{{- if and .cron .prompt -}}
{{- $out = append $out (dict "name" (printf "%s%s" (include "factory.plannerSchedulePrefix" $) .name) "cron" .cron "prompt" (printf "%s%s" (include "factory.plannerPromptPrefix" $) .name)) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- toJson $out -}}
{{- end }}
