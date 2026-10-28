{{- define "monolith.sttEnabled" -}}
{{- if and .Values.grimoire.enabled .Values.grimoire.play.enabled .Values.grimoire.transcript.enabled -}}true{{- end -}}
{{- end -}}

{{- define "monolith.sttEnv" -}}
{{- if include "monolith.sttEnabled" . -}}
{{- if ne .Values.grimoire.transcript.provider "google" -}}
{{- fail "grimoire.transcript.provider must be google" -}}
{{- end -}}
{{- $google := .Values.grimoire.transcript.google -}}
{{- if not (regexMatch "^[a-z][a-z0-9-]{4,61}[a-z0-9]$" $google.project) -}}
{{- fail "enabled transcription requires an existing Google project id" -}}
{{- end -}}
{{- if not $google.onepassword.itemPath -}}
{{- fail "enabled transcription requires the approved OnePasswordItem path" -}}
{{- end -}}
{{- if not (regexMatch "^[a-zA-Z0-9._-]+$" $google.credentialKey) -}}
{{- fail "invalid transcription credential Secret key" -}}
{{- end -}}
- name: GRIMOIRE_STT_PROVIDER
  value: google
- name: GRIMOIRE_STT_PROJECT
  value: {{ $google.project | quote }}
- name: GOOGLE_APPLICATION_CREDENTIALS
  value: /run/grimoire-stt/credentials.json
{{- end -}}
{{- end -}}
