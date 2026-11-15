{{/* Helpers for the connector (connector*.yaml). */}}

{{/* The connector's name: its ServiceAccount, ConfigMap and Deployment. */}}
{{- define "factory.connectorName" -}}factory-connector{{- end }}

{{/*
Selector labels of the connector's pod. networkPolicy.connector names the same
labels for its peers (kagent, itsaplan, the broker, factory-db).
*/}}
{{- define "factory.connectorSelector" -}}
app.kubernetes.io/name: factory-connector
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/* The connector image, by digest once helm_chart(images) has pinned it. */}}
{{- define "factory.connectorImage" -}}
{{- $i := .Values.connector.image -}}
{{- if $i.digest -}}{{ $i.repository }}@{{ $i.digest }}{{- else -}}{{ $i.repository }}:{{ $i.tag }}{{- end -}}
{{- end }}

{{/* The connector ServiceAccount's TokenReview username. */}}
{{- define "factory.connectorSubject" -}}
system:serviceaccount:{{ .Values.broker.namespace }}:{{ include "factory.connectorName" . }}
{{- end }}

{{/* The kagent controller's address: A2A over HTTP and SessionService over h2c share it. */}}
{{- define "factory.kagentControllerAddress" -}}
{{ .Values.kagent.fullnameOverride | default "kagent" }}-controller.{{ include "factory.kagentNamespace" . }}.svc:{{ .Values.kagent.controller.service.ports.port | default 8083 }}
{{- end }}

{{/* "true" when the connector delivers: the broker mints its GitHub tokens. */}}
{{- define "factory.connectorDelivers" -}}
{{- if and .Values.broker.enabled .Values.broker.githubApp.enabled -}}true{{- end -}}
{{- end }}

{{/* Where an itsaplan agent's key file is mounted. */}}
{{- define "factory.connectorKeyPath" -}}/var/run/secrets/factory-itsaplan/{{ . }}{{- end }}
