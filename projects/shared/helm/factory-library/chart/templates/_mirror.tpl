{{- /* invariant: LIB-7 */ -}}
{{/* This public endpoint is already used by the homelab R2 consumers. It is
policy data, not a credential. The Secret endpoint must agree at runtime. */}}
{{- define "factory-library.r2Host" -}}
7c56b458cd657d96b095c63d181c051f.r2.cloudflarestorage.com
{{- end -}}

{{/* Every mirror Job stops at this deadline. The README's writer rule names
the same value; library_test checks that the two agree. */}}
{{- define "factory-library.mirrorDeadlineSeconds" -}}
900
{{- end -}}

{{- define "factory-library.mirrorScript" -}}
set -eu
# Never log credentials or load a config file from the image.
case "$RCLONE_CONFIG_R2_ENDPOINT" in
  "https://$R2_HOST"|"https://$R2_HOST/") ;;
  *) echo "R2 endpoint does not match the declared egress" >&2; exit 1 ;;
esac
run() { rclone --config /dev/null --s3-no-check-bucket "$@"; }
scratch=$(mktemp -d /tmp/mirror.XXXXXX)
trap 'rm -rf "$scratch"' EXIT
{{- if .prefix }}
# Every write and delete below stays under prefix {{ .prefix }}/ of the R2 bucket.
{{- end }}
{{- if .excludePrefixes }}
# The sources of this release own the excluded prefixes. No listing, copy or delete below includes them.
{{- end }}
# payloads VERB ARGS: rclone VERB for every object except the manifest and the copied keys.
payloads() {
  verb=$1
  shift
  run "$verb" \
    --exclude /{{ .manifestName }} \
{{- range .copyKeys }}
    --exclude /{{ .key }} \
{{- end }}
{{- range .excludePrefixes }}
    --exclude '/{{ . }}/**' \
{{- end }}
    "$@"
}
# 0. Capture the manifest between two listings of Garage. A writer puts each
# payload before the manifest that names it, so the two listings together hold
# every payload that the captured manifest names.
payloads lsf -R --files-only "GARAGE:$GARAGE_BUCKET" > "$scratch/before"
# Listing failure is an error, not proof that the manifest is absent.
manifest=$(run lsf "GARAGE:$GARAGE_BUCKET" --files-only --max-depth 1 --include /{{ .manifestName }})
if [ "$manifest" = {{ .manifestName }} ]; then
  run cat "GARAGE:$GARAGE_BUCKET/{{ .manifestName }}" > "$scratch/{{ .manifestName }}"
fi
payloads lsf -R --files-only "GARAGE:$GARAGE_BUCKET" > "$scratch/after"
LC_ALL=C sort -u -o "$scratch/keep" "$scratch/before" "$scratch/after"
# 1. Upload payloads and delete nothing, so the manifest that R2 still holds
# keeps every object it names. A failed copy stops here.
payloads copy "GARAGE:$GARAGE_BUCKET" "{{ .dest }}" \
  --header-upload "Cache-Control: public, max-age=31536000, immutable"
{{- if .repairCacheKey }}
# Copy skips matching bytes, even if the R2 object's metadata is old.
# Repair only the exact content-addressed key while the captured manifest uses it.
repair_key={{ .repairCacheKey | quote }}
if [ "$manifest" = {{ .manifestName }} ] &&
   grep -Fq "\"$repair_key\"" "$scratch/{{ .manifestName }}" &&
   grep -Fxq "$repair_key" "$scratch/keep"; then
  metadata=$(run lsjson --stat --metadata "{{ .dest }}/$repair_key")
  if ! printf '%s\n' "$metadata" | grep -Eq '"cache-control"[[:space:]]*:[[:space:]]*"public, max-age=31536000, immutable"'; then
    run copyto "{{ .dest }}/$repair_key" "$scratch/cache-repair.json"
    expected=${repair_key##*-}
    printf '%s  %s\n' "${expected%.json}" "$scratch/cache-repair.json" | sha256sum -c - >/dev/null
    run copyto "$scratch/cache-repair.json" "{{ .dest }}/$repair_key" --ignore-times --header-upload "Cache-Control: public, max-age=31536000, immutable"
  fi
fi
{{- end }}
payloads lsf -R --files-only "{{ .dest }}" > "$scratch/r2"
LC_ALL=C sort -o "$scratch/r2" "$scratch/r2"
LC_ALL=C comm -23 "$scratch/keep" "$scratch/r2" > "$scratch/missing"
if [ -s "$scratch/missing" ]; then
  # The writer removed a listed payload before the copy, and the captured
  # manifest can name it. The next run captures a new manifest.
  echo "a listed payload left Garage before the copy; R2 keeps its manifest" >&2
  exit 1
fi
# 2. Publish the captured manifest after every object it names.
if [ "$manifest" = {{ .manifestName }} ]; then
  run copyto "$scratch/{{ .manifestName }}" "{{ .dest }}/{{ .manifestName }}" \
    --ignore-times --header-upload "Cache-Control: public, max-age=60"
else
  # No listing includes this key, so remove it separately when Garage lacks it.
  run delete "{{ .dest }}" --include /{{ .manifestName }}
fi
{{- range .copyKeys }}
# A copied key comes from another bucket of this chart. Its writer replaces it
# in place, so it gets the manifest's short cache.
key=$(run lsf "{{ .remote }}:{{ .bucket }}" --files-only --max-depth 1 --include /{{ .key }})
if [ "$key" = {{ .key }} ]; then
  run copyto "{{ .remote }}:{{ .bucket }}/{{ .key }}" "{{ $.dest }}/{{ .key }}" \
    --ignore-times --header-upload "Cache-Control: public, max-age=60"
else
  run delete "{{ $.dest }}" --include /{{ .key }}
fi
{{- end }}
# 3. Only now delete the R2 payloads that neither Garage listing holds. Both
# listings hold every object that the published manifest names, also when the
# writer removes them during this run. Those objects go in a later run.
LC_ALL=C comm -13 "$scratch/keep" "$scratch/r2" > "$scratch/delete"
if [ -s "$scratch/delete" ]; then
  # The list holds exact keys, and --files-from-raw takes no other filter.
  run delete "{{ .dest }}" --files-from-raw "$scratch/delete"
fi
{{- end -}}

{{- define "factory-library.mirror" -}}
{{- $public := .public | default dict -}}
{{- include "factory-library.keys" (dict "path" "public bucket" "value" $public "allowed" (list "r2Bucket" "schedule" "itemPath" "image" "suspend" "manifestName" "copyKeys" "sources")) -}}
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
{{/* Each source is a Garage bucket of another chart. This release mirrors it
under its own prefix, so no other namespace holds the R2 credential. A prefix
is one path segment, so a source cannot reach above or beside it. */}}
{{- $sources := $public.sources | default (list) -}}
{{- if not (kindIs "slice" $sources) -}}{{- fail "factory-library: public.sources must be a list" -}}{{- end -}}
{{- $segment := "^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$" -}}
{{- $prefixes := list -}}
{{- $seenSources := dict -}}
{{- range $source := $sources -}}
{{- if not (kindIs "map" $source) -}}{{- fail "factory-library: public.sources entries must be maps" -}}{{- end -}}
{{- include "factory-library.keys" (dict "path" "public.sources entry" "value" $source "allowed" (list "bucket" "r2Prefix" "manifestName" "schedule" "suspend" "repairCacheKey")) -}}
{{- if not (hasKey $source "bucket") -}}{{- fail "factory-library: public.sources bucket is required" -}}{{- end -}}
{{- $from := $source.bucket -}}
{{- if or (not (kindIs "string" $from)) (not (regexMatch "^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$" $from)) (eq $from $.bucket) (hasKey $seenSources (printf "bucket/%s" $from)) -}}{{- fail "factory-library: public.sources buckets must be unique Garage bucket names other than the mirrored bucket" -}}{{- end -}}
{{- $_ := set $seenSources (printf "bucket/%s" $from) true -}}
{{- if not (hasKey $source "r2Prefix") -}}{{- fail "factory-library: public.sources r2Prefix is required" -}}{{- end -}}
{{- $prefix := $source.r2Prefix -}}
{{- if or (not (kindIs "string" $prefix)) (not (regexMatch $segment $prefix)) -}}{{- fail "factory-library: public.sources r2Prefix must be one path segment of lowercase letters, digits and -" -}}{{- end -}}
{{- if hasKey $seenSources (printf "prefix/%s" $prefix) -}}{{- fail "factory-library: public.sources r2Prefix values must be unique" -}}{{- end -}}
{{- $_ := set $seenSources (printf "prefix/%s" $prefix) true -}}
{{- if and (hasKey $source "suspend") (not (kindIs "bool" $source.suspend)) -}}{{- fail "factory-library: public.sources suspend must be a boolean" -}}{{- end -}}
{{- if not (has (get $source "manifestName" | default "manifest.json") (list "manifest.json" "latest.json")) -}}{{- fail "factory-library: public.sources manifestName must be manifest.json or latest.json" -}}{{- end -}}
{{- if hasKey $source "repairCacheKey" -}}
{{- $key := $source.repairCacheKey -}}
{{- if or (not (kindIs "string" $key)) (not (regexMatch "^[a-z0-9][a-z0-9-]*(/[a-z0-9][a-z0-9-]*)*-[a-f0-9]{64}[.]json$" $key)) -}}{{- fail "factory-library: public.sources repairCacheKey must be a relative, SHA-256-named JSON payload" -}}{{- end -}}
{{- end -}}
{{- $prefixes = append $prefixes $prefix -}}
{{- end -}}
{{- $path := get $public "itemPath" -}}
{{- if or (not (kindIs "string" $path)) (and $path (not (regexMatch "^vaults/[^/]+/items/[^/]+$" $path))) -}}{{- fail "factory-library: public.itemPath must be vaults/<vault>/items/<item>" -}}{{- end -}}
{{/* rclone 1.76.0, upstream multi-platform index; see README for provenance. */}}
{{- $image := $public.image | default (dict "repository" "docker.io/rclone/rclone" "digest" "sha256:7cbeec24a44f206067597c0368be4de90ae19a64d5113c7d1938bd737a81e466") -}}
{{- include "factory-library.keys" (dict "path" "mirror image" "value" $image "allowed" (list "repository" "digest")) -}}
{{- if ne $image.repository "docker.io/rclone/rclone" -}}{{- fail "factory-library: mirror image.repository must be docker.io/rclone/rclone" -}}{{- end -}}
{{/* The root sync excludes every source prefix, so it never deletes their objects. */}}
{{ include "factory-library.mirrorJob" (dict "root" .root "name" (printf "%s-mirror" .base) "bucket" .bucket "r2Bucket" $r2Bucket "prefix" "" "excludePrefixes" $prefixes "itemPath" $path "image" $image "manifestName" $manifest "copy" $copy "copySecrets" $copySecrets "schedule" $public.schedule "suspend" $public.suspend) }}
{{- range $source := $sources }}
{{ include "factory-library.mirrorJob" (dict "root" $.root "name" (printf "%s-%s-mirror" $.root.Release.Name $source.r2Prefix) "bucket" $source.bucket "r2Bucket" $r2Bucket "prefix" $source.r2Prefix "excludePrefixes" (list) "itemPath" $path "image" $image "manifestName" (get $source "manifestName" | default "manifest.json") "copy" (list) "copySecrets" (dict) "schedule" $source.schedule "suspend" $source.suspend "repairCacheKey" $source.repairCacheKey) }}
{{- end }}
{{- end -}}

{{/* One CronJob with its own read-only Garage key, R2 Secret and admission
guard. Its R2 writes go to the bucket root, or only under .prefix. */}}
{{- define "factory-library.mirrorJob" -}}
{{- if and (not .itemPath) (not .suspend) -}}{{- fail "factory-library: public.itemPath is required for an active mirror" -}}{{- end -}}
{{- $name := .name -}}
{{- $r2Secret := printf "%s-r2" $name -}}
{{- $garageSecret := printf "%s-s3" $name -}}
{{- include "factory-library.name" $name -}}
{{- include "factory-library.name" $r2Secret -}}
{{- if hasKey (.root.Values.workloads | default dict) $name -}}{{- fail (printf "factory-library: reserved mirror workload name %s" $name) -}}{{- end -}}
{{- range $secret := list $r2Secret $garageSecret -}}
{{- if hasKey ($.root.Values.secrets | default dict) $secret -}}{{- fail (printf "factory-library: reserved mirror secret name %s" $secret) -}}{{- end -}}
{{- end -}}
{{- $dest := "R2:$R2_BUCKET" -}}
{{- if .prefix -}}{{- $dest = printf "R2:$R2_BUCKET/%s" .prefix -}}{{- end -}}
{{- $env := list -}}
{{- range $key, $value := dict "GARAGE_BUCKET" .bucket "R2_BUCKET" .r2Bucket "R2_HOST" (include "factory-library.r2Host" .) "RCLONE_CONFIG_GARAGE_TYPE" "s3" "RCLONE_CONFIG_GARAGE_PROVIDER" "Other" "RCLONE_CONFIG_GARAGE_FORCE_PATH_STYLE" "true" "RCLONE_CONFIG_R2_TYPE" "s3" "RCLONE_CONFIG_R2_PROVIDER" "Cloudflare" "RCLONE_CONFIG_R2_REGION" "auto" "RCLONE_CONFIG_R2_FORCE_PATH_STYLE" "true" -}}
{{- $env = append $env (dict "name" $key "value" $value) -}}
{{- end -}}
{{- range $key, $field := dict "ACCESS_KEY_ID" "AWS_ACCESS_KEY_ID" "SECRET_ACCESS_KEY" "AWS_SECRET_ACCESS_KEY" "ENDPOINT" "AWS_ENDPOINT_URL" "REGION" "AWS_REGION" -}}
{{- $env = append $env (dict "name" (printf "RCLONE_CONFIG_GARAGE_%s" $key) "valueFrom" (dict "secretKeyRef" (dict "name" $garageSecret "key" $field))) -}}
{{- end -}}
{{- range $from, $remote := .copySecrets -}}
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
{{- $workload := dict "kind" "CronJob" "schedule" (.schedule | default "* * * * *") "concurrencyPolicy" "Forbid" "backoffLimit" 1 "image" .image "containerName" "mirror" "resources" (dict "requests" (dict "cpu" "10m" "memory" "64Mi") "limits" (dict "memory" "256Mi")) "command" (list "/bin/sh" "-c" (include "factory-library.mirrorScript" (dict "manifestName" .manifestName "copyKeys" .copy "dest" $dest "prefix" .prefix "excludePrefixes" .excludePrefixes "repairCacheKey" .repairCacheKey))) "env" $env "writablePaths" (list (dict "name" "tmp" "mountPath" "/tmp" "sizeLimit" "64Mi")) "network" (dict "egress" (list (dict "peer" "garage" "port" 3900) (dict "peer" "r2-s3" "fqdn" (include "factory-library.r2Host" .) "port" 443))) -}}
{{ include "factory-library.bucketSecret" (dict "root" .root "name" $garageSecret "bucket" .bucket "access" "read") }}
{{- range $from, $remote := .copySecrets }}
{{ include "factory-library.bucketSecret" (dict "root" $.root "name" (printf "%s-%s-s3" $name $from) "bucket" $from "access" "read") }}
{{- end }}
{{ include "factory-library.mirrorAdmission" (dict "root" .root "name" $name "secret" $r2Secret) }}
{{- if .itemPath }}
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
  itemPath: {{ .itemPath }}
{{- end }}
{{ include "factory-library.cronjob" (dict "root" .root "name" $name "workload" $workload "mirror" true "suspend" (.suspend | default false)) }}
{{- end -}}
