{{- define "monolith-agents.fullname" -}}{{ include "homelab.fullname" . }}{{- end }}
{{- define "monolith-agents.labels" -}}{{ include "homelab.labels" . }}{{- end }}
{{- define "monolith-agents.selectorLabels" -}}{{ include "homelab.selectorLabels" . }}{{- end }}
{{- define "monolith-agents.serviceAccountName" -}}{{ include "homelab.serviceAccountName" . }}{{- end }}
