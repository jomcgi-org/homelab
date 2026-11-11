{{- define "monolith-public.fullname" -}}{{ include "homelab.fullname" . }}{{- end }}
{{- define "monolith-public.labels" -}}{{ include "homelab.labels" . }}{{- end }}
{{- define "monolith-public.selectorLabels" -}}{{ include "homelab.selectorLabels" . }}{{- end }}
{{- define "monolith-public.serviceAccountName" -}}{{ include "homelab.serviceAccountName" . }}{{- end }}
{{- define "monolith-public.workerDenyAuthorization" -}}
authorization:
  defaultAction: Allow
  rules:
    # Cloudflare documents that same-zone Workers can alter x-real-ip, which
    # becomes CF-Connecting-IP. CF-Worker is added to every fetch subrequest.
    - name: deny-same-zone-workers
      action: Deny
      principal:
        headers:
          - name: CF-Worker
            values:
              - {{ .Values.cfIngress.public.clientIdentity.cloudflareZone | quote }}
    # Cross-zone Worker traffic receives this fixed address rather than the
    # visitor address. Reject it instead of treating all Workers as one client.
    - name: deny-cross-zone-workers
      action: Deny
      principal:
        headers:
          - name: {{ .Values.cfIngress.public.clientIdentity.clientHeader.name | quote }}
            values:
              - {{ .Values.cfIngress.public.clientIdentity.crossZoneWorkerAddress | quote }}
{{- end }}
