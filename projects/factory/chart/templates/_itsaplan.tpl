{{/* Helpers for itsaplan (itsaplan*.yaml). */}}

{{/* The prefix of every itsaplan object's name. */}}
{{- define "factory.itsaplanName" -}}factory-itsaplan{{- end }}

{{/* Selector labels of one itsaplan component's pods. */}}
{{- define "factory.itsaplanSelector" -}}
app.kubernetes.io/name: factory-itsaplan
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{/* Public origins: the web app's, and the api's with its path. */}}
{{- define "factory.itsaplanAppURL" -}}https://{{ .Values.itsaplan.host }}{{- end }}
{{- define "factory.itsaplanAPIURL" -}}https://{{ .Values.itsaplan.host }}{{ .Values.itsaplan.apiPath }}{{- end }}

{{/* The api inside the cluster. */}}
{{- define "factory.itsaplanInternalURL" -}}
http://{{ include "factory.itsaplanName" . }}-api.{{ .Values.itsaplan.namespace }}.svc:3000
{{- end }}

{{/* The Secret holding one bootstrap agent's API key. */}}
{{- define "factory.itsaplanAgentSecret" -}}factory-itsaplan-agent-{{ . }}{{- end }}

{{/* Keep annotations for state that must outlive the Application. */}}
{{- define "factory.itsaplanKeep" -}}
argocd.argoproj.io/sync-options: Prune=false,Delete=false
helm.sh/resource-policy: keep
{{- end }}

{{/* The restricted security context every itsaplan container runs with. */}}
{{- define "factory.itsaplanContainerSecurity" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: [ALL]
{{- end }}

{{- define "factory.itsaplanPodSecurity" -}}
runAsNonRoot: true
runAsUser: 65532
runAsGroup: 65532
seccompProfile:
  type: RuntimeDefault
{{- end }}

{{/*
The api's and worker's environment beyond the ConfigMap: the generated
secrets, the database and the S3 key, each read by key so a pod waits for a
missing value instead of starting without it.
*/}}
{{- define "factory.itsaplanSecretEnv" -}}
{{- $i := .Values.itsaplan }}
{{- $main := include "factory.itsaplanName" . }}
- name: BETTER_AUTH_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ $main }}
      key: BETTER_AUTH_SECRET
- name: APP_ENCRYPTION_KEY
  valueFrom:
    secretKeyRef:
      name: {{ $main }}
      key: APP_ENCRYPTION_KEY
- name: DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ $i.database.secretName }}
      key: DATABASE_URL
{{- range $env, $key := dict "S3_ACCESS_KEY_ID" "AWS_ACCESS_KEY_ID" "S3_SECRET_ACCESS_KEY" "AWS_SECRET_ACCESS_KEY" "S3_ENDPOINT" "AWS_ENDPOINT_URL" "S3_REGION" "AWS_REGION" "S3_BUCKET" "BUCKET" }}
- name: {{ $env }}
  valueFrom:
    secretKeyRef:
      name: {{ $i.s3.secretName }}
      key: {{ $key }}
{{- end }}
{{- end }}
