{{- define "monolith-public.fullname" -}}{{ include "homelab.fullname" . }}{{- end }}
{{- define "monolith-public.labels" -}}{{ include "homelab.labels" . }}{{- end }}
{{- define "monolith-public.selectorLabels" -}}{{ include "homelab.selectorLabels" . }}{{- end }}
{{- define "monolith-public.serviceAccountName" -}}{{ include "homelab.serviceAccountName" . }}{{- end }}
