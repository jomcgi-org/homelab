{{- define "factory-library.networkRule" -}}
{{- $rule := .rule -}}
{{- include "factory-library.keys" (dict "path" "network rule" "value" $rule "allowed" (list "peer" "fqdn" "port" "protocol")) -}}
{{- $peer := required "factory-library: network peer name is required" $rule.peer -}}
{{- $port := required (printf "factory-library: peer %s: an explicit port is required" $peer) $rule.port | toString -}}
{{- if or (not (regexMatch "^[0-9]+$" $port)) (lt (int $port) 1) (gt (int $port) 65535) -}}
{{- fail (printf "factory-library: peer %s: port must be between 1 and 65535" $peer) -}}
{{- end -}}
{{- $protocol := $rule.protocol | default "TCP" -}}
{{- if not (has $protocol (list "TCP" "UDP" "SCTP")) -}}{{- fail "factory-library: protocol must be TCP, UDP or SCTP" -}}{{- end -}}
{{- $peers := include "factory-library.peers" . | fromYaml -}}
{{- $prefix := ternary "from" "to" (eq .direction "ingress") -}}
{{- if $rule.fqdn -}}
{{- if ne .direction "egress" -}}{{- fail "factory-library: FQDN peers are egress only" -}}{{- end -}}
{{- if or (hasKey $peers $peer) (has $peer (list "world" "0.0.0.0/0" "::/0" "kube-apiserver")) -}}
{{- fail "factory-library: an FQDN peer must have its own explicit name" -}}
{{- end -}}
{{- if not (regexMatch "^([a-z0-9*]|[a-z0-9*][a-z0-9.*-]*[a-z0-9*])$" $rule.fqdn) -}}{{- fail "factory-library: invalid FQDN" -}}{{- end -}}
{{- if contains "*" $rule.fqdn -}}
{{- $exception := printf "network.fqdn:%s" $rule.fqdn -}}
{{- if not (include "factory-library.exception" (dict "root" .root "name" .name "default" $exception)) -}}
{{- fail (printf "factory-library: wildcard FQDN %s requires exception %s" $rule.fqdn $exception) -}}
{{- end }}
toFQDNs:
  - matchPattern: {{ $rule.fqdn | quote }}
{{- else }}
toFQDNs:
  - matchName: {{ $rule.fqdn | quote }}
{{- end -}}
{{- else if hasKey $peers $peer }}
{{ $prefix }}Endpoints:
  - matchLabels:
      {{- toYaml (index $peers $peer) | nindent 6 }}
{{- else if has $peer (list "world" "0.0.0.0/0" "::/0") -}}
{{- if not (include "factory-library.exception" (dict "root" .root "name" .name "default" (printf "network.%s" $peer))) -}}
{{- fail (printf "factory-library: peer %s requires exception network.%s" $peer $peer) -}}
{{- end -}}
{{- if eq $peer "world" }}
{{ $prefix }}Entities: [world]
{{- else }}
{{ $prefix }}CIDR: [{{ $peer | quote }}]
{{- end -}}
{{- else if eq $peer "kube-apiserver" -}}
{{- if or (ne .direction "egress") (not .workload.kubernetesApi) -}}
{{- fail "factory-library: kube-apiserver egress requires kubernetesApi: true" -}}
{{- end }}
toEntities: [kube-apiserver]
{{- else -}}
{{- fail (printf "factory-library: unknown peer %s" $peer) -}}
{{- end }}
toPorts:
  - ports:
      - port: {{ $port | quote }}
        protocol: {{ $protocol }}
{{- end -}}

{{- define "factory-library.networkPolicy" -}}
{{- include "factory-library.validateExceptions" .root -}}
{{- $network := deepCopy (.workload.network | default dict) -}}
{{- if and .root.Values.buckets (not .secretInitializer) (not .mirror) -}}
{{- $_ := set $network "egress" (append ($network.egress | default list) (dict "peer" "garage" "port" 3900)) -}}
{{- end -}}
{{- include "factory-library.keys" (dict "path" "network" "value" $network "allowed" (list "ingress" "egress")) }}
---
apiVersion: cilium.io/v2
kind: CiliumNetworkPolicy
metadata:
  name: {{ .name }}
  {{- if .secretInitializer }}
  annotations:
    argocd.argoproj.io/sync-wave: "-2"
  {{- else }}
  {{- include "factory-library.annotations" . | nindent 2 }}
  {{- end }}
spec:
  endpointSelector:
    matchLabels:
      {{- include "factory-library.selector" . | nindent 6 }}
  {{- if not (include "factory-library.exception" (dict "root" .root "name" .name "default" "network.defaultDeny")) }}
  enableDefaultDeny:
    ingress: true
    egress: true
  {{- end }}
  {{- range $direction := list "ingress" "egress" }}
  {{- with index $network $direction }}
  {{ $direction }}:
    {{- range . }}
    - {{ include "factory-library.networkRule" (dict "root" $.root "name" $.name "workload" $.workload "direction" $direction "rule" .) | trim | nindent 6 | trim }}
    {{- end }}
    {{- if and $.mirror (eq $direction "egress") }}
    - toEndpoints:
        - matchLabels:
            k8s:io.kubernetes.pod.namespace: kube-system
            k8s:k8s-app: kube-dns
      toPorts:
        - ports:
            - port: "53"
              protocol: ANY
          rules:
            dns:
              - matchPattern: "*"
    {{- end }}
  {{- end }}
  {{- end }}
{{- end -}}
