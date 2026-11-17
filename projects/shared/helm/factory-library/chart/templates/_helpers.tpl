{{/* Public entry point. Each workload owns its ServiceAccount and policy. */}}
{{- define "factory-library.all" -}}
{{- include "factory-library.validateExceptions" . -}}
{{- range $name, $workload := .Values.workloads }}
{{- $ctx := dict "root" $ "name" $name "workload" $workload }}
{{- $kind := $workload.kind | default "Deployment" }}
{{- if eq $kind "Deployment" }}
{{ include "factory-library.deployment" $ctx }}
{{- else if eq $kind "CronJob" }}
{{ include "factory-library.cronjob" $ctx }}
{{- else if eq $kind "Job" }}
{{ include "factory-library.job" $ctx }}
{{- else }}
{{- fail (printf "factory-library: workload %s: unknown kind %s" $name $kind) }}
{{- end }}
{{- end }}
{{ include "factory-library.secrets" . }}
{{ include "factory-library.buckets" . }}
{{- end -}}

{{/* Reject misspellings and raw Kubernetes overrides instead of ignoring them. */}}
{{- define "factory-library.keys" -}}
{{- range $key, $_ := .value -}}
{{- if not (has $key $.allowed) -}}
{{- fail (printf "factory-library: %s: unsupported field %s" $.path $key) -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "factory-library.selector" -}}
{{- include "factory-library.name" .name -}}
app.kubernetes.io/name: {{ .name | quote }}
{{- end -}}

{{- define "factory-library.name" -}}
{{- if or (gt (len .) 63) (not (regexMatch "^[a-z0-9]([-a-z0-9]*[a-z0-9])?$" .)) -}}
{{- fail (printf "factory-library: invalid resource name %q (use a DNS label of at most 63 characters)" .) -}}
{{- end -}}
{{- end -}}

{{- define "factory-library.image" -}}
{{- $image := .workload.image | default .root.Values.image | default dict -}}
{{- $repo := required (printf "factory-library: workload %s: image.repository is required" .name) $image.repository -}}
{{- $digest := required (printf "factory-library: workload %s: image.digest is required (tags are not deployable)" .name) $image.digest -}}
{{- if not (regexMatch "^sha256:[a-f0-9]{64}$" $digest) -}}
{{- fail (printf "factory-library: workload %s: image.digest must be sha256 followed by 64 lowercase hex digits" .name) -}}
{{- end -}}
{{- if or (contains "@" $repo) (contains ":" (last (splitList "/" $repo))) -}}
{{- fail (printf "factory-library: workload %s: image.repository must not contain a tag or digest" .name) -}}
{{- end -}}
{{- printf "%s@%s" $repo $digest -}}
{{- end -}}

{{/* An exception is the only input that can change a security default. */}}
{{- define "factory-library.validateExceptions" -}}
{{- $seen := dict -}}
{{- range .Values.exceptions -}}
{{- include "factory-library.keys" (dict "path" "exception" "value" . "allowed" (list "workload" "default" "reason" "value")) -}}
{{- $name := required "factory-library: exception.workload is required" .workload -}}
{{- $default := required "factory-library: exception.default is required" .default -}}
{{- if not (trim (default "" .reason)) -}}{{- fail "factory-library: exception.reason is required" -}}{{- end -}}
{{- if not (hasKey ($.Values.workloads | default dict) $name) -}}
{{- fail (printf "factory-library: exception names unknown workload %s" $name) -}}
{{- end -}}
{{- if not (or (has $default (list "runAsUser" "runAsGroup" "readOnlyRootFilesystem" "network.defaultDeny" "network.world" "network.0.0.0.0/0" "network.::/0")) (hasPrefix "network.fqdn:" $default)) -}}
{{- fail (printf "factory-library: unsupported exception default %s" $default) -}}
{{- end -}}
{{- if has $default (list "runAsUser" "runAsGroup") -}}
{{- if or (not (hasKey . "value")) (not (regexMatch "^[1-9][0-9]*$" (toString .value))) -}}
{{- fail (printf "factory-library: exception %s requires a positive numeric value" $default) -}}
{{- end -}}
{{- else if hasKey . "value" -}}
{{- fail (printf "factory-library: exception %s does not accept a value" $default) -}}
{{- end -}}
{{- $key := printf "%s/%s" $name $default -}}
{{- if hasKey $seen $key -}}{{- fail (printf "factory-library: duplicate exception %s" $key) -}}{{- end -}}
{{- $_ := set $seen $key true -}}
{{- end -}}
{{- end -}}

{{- define "factory-library.exception" -}}
{{- range .root.Values.exceptions -}}
{{- if and (eq .workload $.name) (eq .default $.default) -}}true{{- end -}}
{{- end -}}
{{- end -}}

{{- define "factory-library.securityID" -}}
{{- $value := 65532 -}}
{{- range .root.Values.exceptions -}}
{{- if and (eq .workload $.name) (eq .default $.default) -}}{{- $value = int .value -}}{{- end -}}
{{- end -}}
{{- $value -}}
{{- end -}}

{{/* JSON preserves exact exception names and reasons, including CIDRs and FQDNs. */}}
{{- define "factory-library.annotations" -}}
{{- $exceptions := list -}}
{{- range .root.Values.exceptions -}}
{{- if eq .workload $.name -}}
{{- $exceptions = append $exceptions . -}}
{{- end -}}
{{- end -}}
{{- if $exceptions }}
annotations:
  factory.jomcgi.dev/exceptions: {{ $exceptions | toJson | quote }}
{{- end -}}
{{- end -}}
