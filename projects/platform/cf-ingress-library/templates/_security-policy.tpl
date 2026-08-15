{{/*
cf-ingress.security-policy generates a SecurityPolicy for JWT validation
against Cloudflare Access.

Usage:
  {{- include "cf-ingress.security-policy" . }}

Required values:
  name: HTTPRoute name to target
  team: Cloudflare Access team name (default: "jomcgi")
*/}}
{{- define "cf-ingress.security-policy" -}}
apiVersion: gateway.envoyproxy.io/v1alpha1
kind: SecurityPolicy
metadata:
  name: {{ .name }}-cf-access
spec:
  targetRefs:
    - group: gateway.networking.k8s.io
      kind: HTTPRoute
      name: {{ .name }}
  jwt:
    providers:
      - name: cloudflare-access
        issuer: https://{{ .team | default "jomcgi" }}.cloudflareaccess.com
        remoteJWKS:
          # Envoy Gateway's own default, declared explicitly for the same
          # ServerSideApply field-ownership reason as the HTTPRoute defaults.
          # targetRefs above already spelled its group/kind out and showed no
          # drift while this key did, on the same object, which is what
          # identified the mechanism.
          cacheDuration: 300s
          uri: https://{{ .team | default "jomcgi" }}.cloudflareaccess.com/cdn-cgi/access/certs
        extractFrom:
          headers:
            - name: Cf-Access-Jwt-Assertion
        claimToHeaders:
          - claim: email
            header: X-Auth-Email
{{- end }}
