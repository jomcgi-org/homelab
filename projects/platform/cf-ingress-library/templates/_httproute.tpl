{{/*
cf-ingress.httproute generates an HTTPRoute with the ingress-tier label.

Usage:
  {{- include "cf-ingress.httproute" . }}

Required values:
  name: resource name
  tier: "trusted" or "public"
  hostname: "app.jomcgi.dev"
  serviceName: "my-service"
  servicePort: 80
  gateway:
    name: "cloudflare-ingress"
    namespace: "envoy-gateway-system"

Optional values:
  pathPrefix: "/todo" (defaults to "/")
  rewritePrefix: "/public/" (rewrites matched pathPrefix to this value)
*/}}
{{- define "cf-ingress.httproute" -}}
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: {{ .name }}
  labels:
    ingress-tier: {{ .tier }}
spec:
  # group/kind here, and group/kind/weight on backendRefs below, are the Gateway
  # API schema defaults. They are spelled out rather than left implicit because
  # the consuming Applications set ServerSideApply=true, and SSA diffs on field
  # OWNERSHIP: a field the chart never declares is absent from ArgoCD's
  # predicted state but present in live once the apiserver defaults it, so the
  # app reports OutOfSync forever and selfHeal re-syncs without ever converging.
  # Client-side apply hides this (its 3-way merge treats undeclared fields as
  # someone else's), which is why the non-SSA consumers of this library stayed
  # Synced against the identical live objects.
  parentRefs:
    - group: gateway.networking.k8s.io
      kind: Gateway
      name: {{ .gateway.name }}
      namespace: {{ .gateway.namespace }}
  hostnames:
    - {{ .hostname | quote }}
  rules:
    - matches:
        - path:
            type: PathPrefix
            value: {{ .pathPrefix | default "/" }}
      {{- if .rewritePrefix }}
      filters:
        - type: URLRewrite
          urlRewrite:
            path:
              type: ReplacePrefixMatch
              replacePrefixMatch: {{ .rewritePrefix }}
      {{- end }}
      backendRefs:
        - group: ""
          kind: Service
          name: {{ .serviceName }}
          port: {{ .servicePort }}
          weight: 1
{{- end }}
