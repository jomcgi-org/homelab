{{/* Reviewer identities and endpoints are derived from the same values everywhere. */}}
{{- define "factory.reviewerName" -}}factory-reviewer{{- end }}

{{- define "factory.reviewerSelector" -}}
app.kubernetes.io/name: factory-reviewer
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "factory.reviewerSubject" -}}
system:serviceaccount:{{ .Values.broker.namespace }}:{{ include "factory.reviewerName" . }}
{{- end }}

{{- define "factory.reviewerImage" -}}
{{- $i := .Values.reviewer.image -}}
{{- if $i.digest -}}{{ $i.repository }}@{{ $i.digest }}{{- else -}}{{ $i.repository }}:{{ $i.tag }}{{- end -}}
{{- end }}

{{/* Each App writes github-git, so reviews need a separate namespace/atespace. */}}
{{- define "factory.reviewerNamespace" -}}
{{- $b := index (.root.Values.reviewer.bindings | default dict) .binding.name | default dict -}}
{{- $b.namespace | default (printf "%s-review" .binding.namespace) -}}
{{- end }}

{{- define "factory.reviewerConfig" -}}
{{- $c := .Values.reviewer }}
{{- $bindings := list }}
{{- range $r := .Values.repositories }}
{{- $b := index ($c.bindings | default dict) $r.name | default dict }}
{{- $binding := dict
  "name" $r.name
  "owner" $r.owner
  "repo" $r.repo
  "baseBranch" $r.baseBranch
  "kagentNamespace" (include "factory.reviewerNamespace" (dict "root" $ "binding" $r))
  "kagentAgent" (include "factory.bindingAgentName" (dict "name" $r.name "agent" "reviewer"))
  "enqueueAuthors" $c.enqueueAuthors
  "humanApprovers" $c.humanApprovers
}}
{{- range $k, $v := omit $b "namespace" }}
{{- $_ := set $binding $k $v }}
{{- end }}
{{- $bindings = append $bindings $binding }}
{{- end }}
{{- dict
  "brokerURL" (printf "http://%s.%s.svc:%d" (include "factory.brokerName" .) .Values.broker.namespace (int .Values.broker.adminPort))
  "brokerTokenFile" "/var/run/secrets/factory-broker/token"
  "kagentURL" (printf "http://%s" (include "factory.kagentControllerAddress" .))
  "kagentTokenFile" "/var/run/secrets/kagent/token"
  "metricsAddr" (printf ":%d" (int $c.metricsPort))
  "appId" (int $c.appId)
  "checkName" $c.checkName
  "concurrency" (int $c.concurrency)
  "intervals" $c.intervals
  "ruleIds" $c.ruleIds
  "bindings" $bindings
  | toYaml
}}
{{- end }}
