{{/* This public endpoint is already used by the homelab R2 consumers. It is
policy data, not a credential. The Secret endpoint must agree at runtime. */}}
{{- define "factory-library.r2Host" -}}
7c56b458cd657d96b095c63d181c051f.r2.cloudflarestorage.com
{{- end -}}

{{- define "factory-library.mirrorScript" -}}
set -eu
# Never log credentials or load a config file from the image.
case "$RCLONE_CONFIG_R2_ENDPOINT" in
  "https://$R2_HOST"|"https://$R2_HOST/") ;;
  *) echo "R2 endpoint does not match the declared egress" >&2; exit 1 ;;
esac
run() { rclone --config /dev/null --s3-no-check-bucket "$@"; }
# Upload payloads before publishing the manifest. A failed sync stops here.
run sync "GARAGE:$GARAGE_BUCKET" "R2:$R2_BUCKET" \
  --exclude /{{ .manifestName }} \
{{- range .copyKeys }}
  --exclude /{{ .key }} \
{{- end }}
  --header-upload "Cache-Control: public, max-age=31536000, immutable"
# Listing failure is an error, not proof that the manifest is absent.
manifest=$(run lsf "GARAGE:$GARAGE_BUCKET" --files-only --max-depth 1 --include /{{ .manifestName }})
if [ "$manifest" = {{ .manifestName }} ]; then
  run copyto "GARAGE:$GARAGE_BUCKET/{{ .manifestName }}" "R2:$R2_BUCKET/{{ .manifestName }}" \
    --ignore-times --header-upload "Cache-Control: public, max-age=60"
else
  # The sync excludes this key, so remove it separately when Garage lacks it.
  run delete "R2:$R2_BUCKET" --include /{{ .manifestName }}
fi
{{- range .copyKeys }}
# A copied key comes from another bucket of this chart. Its writer replaces it
# in place, so it gets the manifest's short cache.
key=$(run lsf "{{ .remote }}:{{ .bucket }}" --files-only --max-depth 1 --include /{{ .key }})
if [ "$key" = {{ .key }} ]; then
  run copyto "{{ .remote }}:{{ .bucket }}/{{ .key }}" "R2:$R2_BUCKET/{{ .key }}" \
    --ignore-times --header-upload "Cache-Control: public, max-age=60"
else
  run delete "R2:$R2_BUCKET" --include /{{ .key }}
fi
{{- end }}
{{- end -}}

{{- define "factory-library.mirror" -}}
{{- $public := .public | default dict -}}
{{- include "factory-library.keys" (dict "path" "public bucket" "value" $public "allowed" (list "r2Bucket" "schedule" "itemPath" "image" "suspend" "manifestName" "copyKeys")) -}}
{{- if and (hasKey $public "suspend") (not (kindIs "bool" $public.suspend)) -}}{{- fail "factory-library: public.suspend must be a boolean" -}}{{- end -}}
{{- $manifest := get $public "manifestName" | default "manifest.json" -}}
{{- if not (has $manifest (list "manifest.json" "latest.json")) -}}{{- fail "factory-library: public.manifestName must be manifest.json or latest.json" -}}{{- end -}}
{{- $copy := list -}}
{{- $copySecrets := dict -}}
{{- $copyKeys := $public.copyKeys | default (list) -}}
{{- if not (kindIs "slice" $copyKeys) -}}{{- fail "factory-library: public.copyKeys must be a list" -}}{{- end -}}
{{- $buckets := list -}}
{{- range .root.Values.buckets -}}{{- $buckets = append $buckets .name -}}{{- end -}}
{{- $seenKeys := dict -}}
{{- range $entry := $copyKeys -}}
{{- if not (kindIs "map" $entry) -}}{{- fail "factory-library: public.copyKeys entries must be maps of bucket and key" -}}{{- end -}}
{{- include "factory-library.keys" (dict "path" "public.copyKeys entry" "value" $entry "allowed" (list "bucket" "key")) -}}
{{- $from := required "factory-library: public.copyKeys bucket is required" $entry.bucket -}}
{{- $key := required "factory-library: public.copyKeys key is required" $entry.key -}}
{{- if or (eq $from $.bucket) (not (has $from $buckets)) -}}{{- fail "factory-library: public.copyKeys bucket must be another bucket of this chart" -}}{{- end -}}
{{- if or (not (kindIs "string" $key)) (not (regexMatch "^[a-z0-9][a-z0-9-]*\\.json$" $key)) (eq $key $manifest) (hasKey $seenKeys $key) -}}{{- fail "factory-library: public.copyKeys keys must be unique root *.json names other than the manifest name" -}}{{- end -}}
{{- $_ := set $seenKeys $key true -}}
{{- if not (hasKey $copySecrets $from) -}}{{- $_ := set $copySecrets $from (printf "SRC%d" (len $copySecrets)) -}}{{- end -}}
{{- $copy = append $copy (dict "bucket" $from "key" $key "remote" (get $copySecrets $from)) -}}
{{- end -}}
{{- $r2Bucket := required "factory-library: public.r2Bucket is required" $public.r2Bucket -}}
{{- include "factory-library.name" $r2Bucket -}}
{{- if lt (len $r2Bucket) 3 -}}{{- fail "factory-library: public.r2Bucket must be at least 3 characters" -}}{{- end -}}
{{- $path := required "factory-library: public.itemPath is required" $public.itemPath -}}
{{- if not (regexMatch "^vaults/[^/]+/items/[^/]+$" $path) -}}{{- fail "factory-library: public.itemPath must be vaults/<vault>/items/<item>" -}}{{- end -}}
{{- $name := printf "%s-mirror" .base -}}
{{- $r2Secret := printf "%s-r2" $name -}}
{{- $garageSecret := printf "%s-s3" $name -}}
{{- include "factory-library.name" $name -}}
{{- include "factory-library.name" $r2Secret -}}
{{- if hasKey (.root.Values.workloads | default dict) $name -}}{{- fail (printf "factory-library: reserved mirror workload name %s" $name) -}}{{- end -}}
{{- range $secret := list $r2Secret $garageSecret -}}
{{- if hasKey ($.root.Values.secrets | default dict) $secret -}}{{- fail (printf "factory-library: reserved mirror secret name %s" $secret) -}}{{- end -}}
{{- end -}}
{{/* rclone 1.76.0, upstream multi-platform index; see README for provenance. */}}
{{- $image := $public.image | default (dict "repository" "docker.io/rclone/rclone" "digest" "sha256:7cbeec24a44f206067597c0368be4de90ae19a64d5113c7d1938bd737a81e466") -}}
{{- include "factory-library.keys" (dict "path" "mirror image" "value" $image "allowed" (list "repository" "digest")) -}}
{{- if ne $image.repository "docker.io/rclone/rclone" -}}{{- fail "factory-library: mirror image.repository must be docker.io/rclone/rclone" -}}{{- end -}}
{{- $env := list -}}
{{- range $key, $value := dict "GARAGE_BUCKET" .bucket "R2_BUCKET" $r2Bucket "R2_HOST" (include "factory-library.r2Host" .) "RCLONE_CONFIG_GARAGE_TYPE" "s3" "RCLONE_CONFIG_GARAGE_PROVIDER" "Other" "RCLONE_CONFIG_GARAGE_FORCE_PATH_STYLE" "true" "RCLONE_CONFIG_R2_TYPE" "s3" "RCLONE_CONFIG_R2_PROVIDER" "Cloudflare" "RCLONE_CONFIG_R2_REGION" "auto" "RCLONE_CONFIG_R2_FORCE_PATH_STYLE" "true" -}}
{{- $env = append $env (dict "name" $key "value" $value) -}}
{{- end -}}
{{- range $key, $field := dict "ACCESS_KEY_ID" "AWS_ACCESS_KEY_ID" "SECRET_ACCESS_KEY" "AWS_SECRET_ACCESS_KEY" "ENDPOINT" "AWS_ENDPOINT_URL" "REGION" "AWS_REGION" -}}
{{- $env = append $env (dict "name" (printf "RCLONE_CONFIG_GARAGE_%s" $key) "valueFrom" (dict "secretKeyRef" (dict "name" $garageSecret "key" $field))) -}}
{{- end -}}
{{- range $from, $remote := $copySecrets -}}
{{- $copySecret := printf "%s-%s-s3" $name $from -}}
{{- range $key, $value := dict "TYPE" "s3" "PROVIDER" "Other" "FORCE_PATH_STYLE" "true" -}}
{{- $env = append $env (dict "name" (printf "RCLONE_CONFIG_%s_%s" $remote $key) "value" $value) -}}
{{- end -}}
{{- range $key, $field := dict "ACCESS_KEY_ID" "AWS_ACCESS_KEY_ID" "SECRET_ACCESS_KEY" "AWS_SECRET_ACCESS_KEY" "ENDPOINT" "AWS_ENDPOINT_URL" "REGION" "AWS_REGION" -}}
{{- $env = append $env (dict "name" (printf "RCLONE_CONFIG_%s_%s" $remote $key) "valueFrom" (dict "secretKeyRef" (dict "name" $copySecret "key" $field))) -}}
{{- end -}}
{{- end -}}
{{- range $key, $field := dict "ACCESS_KEY_ID" "access-key-id" "SECRET_ACCESS_KEY" "secret-access-key" "ENDPOINT" "s3-api-endpoint" -}}
{{- $env = append $env (dict "name" (printf "RCLONE_CONFIG_R2_%s" $key) "valueFrom" (dict "secretKeyRef" (dict "name" $r2Secret "key" $field))) -}}
{{- end -}}
{{- $workload := dict "kind" "CronJob" "schedule" ($public.schedule | default "* * * * *") "concurrencyPolicy" "Forbid" "backoffLimit" 1 "image" $image "containerName" "mirror" "resources" (dict "requests" (dict "cpu" "10m" "memory" "64Mi") "limits" (dict "memory" "256Mi")) "command" (list "/bin/sh" "-c" (include "factory-library.mirrorScript" (dict "manifestName" $manifest "copyKeys" $copy))) "env" $env "writablePaths" (list (dict "name" "tmp" "mountPath" "/tmp" "sizeLimit" "64Mi")) "network" (dict "egress" (list (dict "peer" "garage" "port" 3900) (dict "peer" "r2-s3" "fqdn" (include "factory-library.r2Host" .) "port" 443))) -}}
{{ include "factory-library.bucketSecret" (dict "root" .root "name" $garageSecret "bucket" .bucket "access" "read") }}
{{- range $from, $remote := $copySecrets }}
{{ include "factory-library.bucketSecret" (dict "root" $.root "name" (printf "%s-%s-s3" $name $from) "bucket" $from "access" "read") }}
{{- end }}
{{ include "factory-library.mirrorAdmission" (dict "root" .root "name" $name "secret" $r2Secret) }}
---
apiVersion: onepassword.com/v1
kind: OnePasswordItem
metadata:
  name: {{ $r2Secret }}
  namespace: {{ .root.Release.Namespace }}
  annotations:
    argocd.argoproj.io/sync-wave: "-2"
type: Opaque
spec:
  itemPath: {{ $path }}
{{ include "factory-library.cronjob" (dict "root" .root "name" $name "workload" $workload "mirror" true "suspend" ($public.suspend | default false)) }}
{{- end -}}
