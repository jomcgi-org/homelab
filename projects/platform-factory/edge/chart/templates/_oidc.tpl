{{/* Shared by the gateway and kagent's route policy. Token forwarding belongs
only in the route policy, since other backends have their own bearer auth. */}}
{{- define "edge.oidc" -}}
{{- $issuerBase := printf "https://%s/application/o" .Values.authentik.hostname -}}
provider:
  issuer: {{ printf "%s/%s/" $issuerBase .Values.authentik.oidc.applicationSlug }}
  authorizationEndpoint: {{ printf "%s/authorize/" $issuerBase }}
  tokenEndpoint: {{ printf "%s/token/" $issuerBase }}
  endSessionEndpoint: {{ printf "%s/%s/end-session/" $issuerBase .Values.authentik.oidc.applicationSlug }}
clientID: {{ .Values.authentik.oidc.clientID }}
clientSecret:
  name: {{ .Values.authentik.oidc.clientSecretName }}
scopes:
  - openid
  - email
  - profile
# Cloudflare terminates TLS, so the request Envoy sees is plain HTTP and
# the default redirect URL (built from X-Forwarded-Proto, which Envoy
# rewrites for an untrusted peer) would be http://.
redirectURL: "https://%REQ(:authority)%/oauth2/callback"
# Envoy's default /logout would shadow an application's own path.
logoutPath: /oauth2/logout
# OAuth2 decrypts the session cookie before the JWT filter reads it.
cookieNames:
  idToken: factory-edge-id-token
cookieConfig:
  sameSite: Lax
# No refresh: when the access token expires (10 minutes, set on the
# factory-edge provider) Envoy sends the browser back to Authentik, which
# re-checks the group bindings. Removing someone from both operator groups
# ends their edge access within that window; an active Authentik session
# makes the round trip invisible to everyone else.
refreshToken: false
{{- end -}}

{{- define "edge.operatorAuthorization" -}}
{{- $issuer := printf "https://%s/application/o/%s/" .Values.authentik.hostname .Values.authentik.oidc.applicationSlug -}}
jwt:
  providers:
    - name: authentik
      issuer: {{ $issuer }}
      audiences:
        - {{ .Values.authentik.oidc.clientID }}
      remoteJWKS:
        uri: {{ printf "%sjwks/" $issuer }}
        # The CRD default; stated so the live policy matches the render.
        cacheDuration: 300s
      claimToHeaders:
        - claim: sub
          header: X-Runbook-User
      extractFrom:
        cookies:
          - factory-edge-id-token
authorization:
  defaultAction: Deny
  rules:
    {{- if .denyT3 }}
    # A missing route-level policy must not inherit operator access.
    # The relay rejects every other spelling of this authority.
    - name: deny-t3-fallback
      action: Deny
      principal:
        headers:
          - name: ":authority"
            values: [t3.jomcgi.dev]
    {{- end }}
    - name: {{ ternary "allow-admins" "allow-operators" (default false .adminOnly) }}
      action: Allow
      principal:
        jwt:
          provider: authentik
          claims:
            - name: groups
              valueType: StringArray
              values:
                - authentik Admins
                {{- if not .adminOnly }}
                - factory-operators
                {{- end }}
{{- end -}}
