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
cookieConfig:
  sameSite: Lax
# No refresh: when the access token expires (10 minutes, set on the
# factory-edge provider) Envoy sends the browser back to Authentik, which
# re-checks the authentik Admins binding. Removing someone from the group
# ends their edge access within that window; an active Authentik session
# makes the round trip invisible to everyone else.
refreshToken: false
{{- end -}}
