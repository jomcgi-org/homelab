{{/*
Name/label helpers for the lakehouse chart. Mirrors projects/monolith/chart's
release-name-as-fullname convention so resource names are stable and the chart
is single-release.
*/}}

{{- define "lakehouse.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Common labels applied to every object. app.kubernetes.io/name is the chart
name; instance is the release; managed-by is helm.
*/}}
{{- define "lakehouse.labels" -}}
app.kubernetes.io/name: lakehouse
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/*
Selector labels — the stable subset used in Deployment selectors / Service
selectors. NEVER add mutable labels (e.g. version) here: selectors are
immutable on Deployments.
*/}}
{{- define "lakehouse.selectorLabels" -}}
app.kubernetes.io/name: lakehouse
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
Shared cluster-service env injected into every worker / quack / dispatcher pod.
Source of truth for the lakehouse's upstream coordinates (Temporal, NATS,
SeaweedFS S3, Iceberg warehouse). Values come from .Values.env so deploy/
overrides can retarget without touching templates. Emitted as a `env:` list
fragment; callers append it after their pod-specific env entries.

Usage:
  env:
    - name: TASK_QUEUE
      value: gap-drain
    {{- include "lakehouse.sharedEnv" . | nindent 12 }}
*/}}
{{- define "lakehouse.sharedEnv" -}}
- name: TEMPORAL_TARGET
  value: {{ .Values.env.temporalTarget | quote }}
- name: NATS_URL
  value: {{ .Values.env.natsUrl | quote }}
- name: SEAWEEDFS_S3_ENDPOINT
  value: {{ .Values.env.seaweedfsS3Endpoint | quote }}
- name: ICEBERG_WAREHOUSE
  value: {{ .Values.env.icebergWarehouse | quote }}
# DuckDB writes its extension cache to $HOME/.duckdb; the containers are
# read-only-rootfs (HOME unset => /.duckdb fails). Point HOME at the writable
# /tmp emptyDir every pod mounts, so httpfs/iceberg/vss INSTALL/LOAD works.
- name: HOME
  value: /tmp
# pyiceberg's S3 FileIO uses pyarrow's AWS C++ SDK, which now defaults to adding
# a streaming trailing checksum (aws-chunked / STREAMING-UNSIGNED-PAYLOAD-TRAILER)
# on PUT. SeaweedFS does NOT decode that framing — it persists the literal chunk
# headers ("88C\r\n{...}\r\n...chunk-signature\r\n\r\n") into the object body,
# corrupting every metadata.json / data file pyiceberg writes (SeaweedFS #6847,
# #6583). Force the SDK back to the pre-checksum behaviour so PUTs are plain.
# DuckDB/httpfs and boto3 are unaffected; the vars are harmless for them.
- name: AWS_REQUEST_CHECKSUM_CALCULATION
  value: when_required
- name: AWS_RESPONSE_CHECKSUM_VALIDATION
  value: when_required
{{- end -}}

{{/*
SeaweedFS S3 credentials env (access key id + secret) sourced from the synced
1Password Secret. SeaweedFS S3 auth is disabled in this cluster, so these are
dummy values, but the worker/quack DuckDB connection still issues
CREATE SECRET ... so wiring real env keeps the secret path identical if auth is
later enabled. See templates/onepassworditem-s3.yaml.
*/}}
{{- define "lakehouse.s3Env" -}}
- name: S3_ACCESS_KEY_ID
  valueFrom:
    secretKeyRef:
      name: {{ include "lakehouse.fullname" . }}-s3
      key: S3_ACCESS_KEY_ID
- name: S3_SECRET_ACCESS_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "lakehouse.fullname" . }}-s3
      key: S3_SECRET_ACCESS_KEY
{{- end -}}
