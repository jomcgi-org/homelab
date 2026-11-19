{{/* Authorizations live in Garage's namespace, outside consumer write access. */}}
{{- define "factory-library.bucketSecret" -}}
{{- include "factory-library.name" .name }}
{{- $claim := printf "garage-claim-%s" (printf "%s/%s" .root.Release.Namespace .name | sha256sum) -}}
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ $claim }}
  namespace: garage
  labels:
    factory.jomcgi.dev/garage-claim: "true"
  annotations:
    argocd.argoproj.io/sync-wave: "-2"
data:
  namespace: {{ .root.Release.Namespace | quote }}
  secretName: {{ .name | quote }}
  bucket: {{ .bucket | quote }}
  access: {{ .access | quote }}
---
apiVersion: v1
kind: Secret
metadata:
  name: {{ .name }}
  labels:
    factory.jomcgi.dev/garage-bucket: {{ .bucket | quote }}
    factory.jomcgi.dev/garage-access: {{ .access | quote }}
  annotations:
    argocd.argoproj.io/sync-wave: "-2"
    argocd.argoproj.io/sync-options: Prune=false,Delete=false
    helm.sh/resource-policy: keep
type: Opaque
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: {{ .name }}
  annotations:
    argocd.argoproj.io/sync-wave: "-2"
rules:
  - apiGroups: [""]
    resources: [secrets]
    resourceNames: [{{ .name | quote }}]
    verbs: [get, update]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: {{ .name }}
  annotations:
    argocd.argoproj.io/sync-wave: "-2"
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: {{ .name }}
subjects:
  - kind: ServiceAccount
    name: garage-init
    namespace: garage
{{- end -}}

{{- define "factory-library.buckets" -}}
{{- $seen := dict -}}
{{- $secretNames := dict -}}
{{- $destinations := dict -}}
{{- range .Values.buckets -}}
{{- include "factory-library.keys" (dict "path" "bucket" "value" . "allowed" (list "name" "access" "public")) -}}
{{- $bucket := required "factory-library: bucket.name is required" .name -}}
{{- include "factory-library.name" $bucket -}}
{{- if or (lt (len $bucket) 3) (hasKey $seen $bucket) -}}{{- fail "factory-library: bucket names must be unique and at least 3 characters" -}}{{- end -}}
{{- $_ := set $seen $bucket true -}}
{{- $access := required "factory-library: bucket.access is required" .access -}}
{{- if not (has $access (list "read" "write" "readwrite")) -}}{{- fail "factory-library: bucket.access must be read, write or readwrite" -}}{{- end -}}
{{- $base := printf "%s-%s" $.Release.Name $bucket -}}
{{- $secret := printf "%s-s3" $base -}}
{{- $names := list $secret -}}
{{- if hasKey . "public" -}}
{{- $names = concat $names (list (printf "%s-mirror-s3" $base) (printf "%s-mirror-r2" $base)) -}}
{{- range ((.public | default dict).copyKeys | default (list)) -}}
{{- if and (kindIs "map" .) (kindIs "string" .bucket) -}}
{{- $copySecret := printf "%s-mirror-%s-s3" $base .bucket -}}
{{- if not (has $copySecret $names) -}}{{- $names = append $names $copySecret -}}{{- end -}}
{{- end -}}
{{- end -}}
{{- $destination := required "factory-library: public.r2Bucket is required" (.public | default dict).r2Bucket -}}
{{/* Mirrors into one R2 bucket need distinct prefixes; the mirror validates each prefix. */}}
{{- $destination = printf "%v/%v" $destination ((.public | default dict).r2Prefix | default "") -}}
{{- if hasKey $destinations $destination -}}{{- fail "factory-library: each R2 bucket and prefix must have only one mirror" -}}{{- end -}}
{{- $_ := set $destinations $destination true -}}
{{- end -}}
{{- range $name := $names -}}
{{- if hasKey $secretNames $name -}}{{- fail (printf "factory-library: bucket Secret name collision %s" $name) -}}{{- end -}}
{{- $_ := set $secretNames $name true -}}
{{- end -}}
{{- if hasKey ($.Values.secrets | default dict) $secret -}}{{- fail (printf "factory-library: reserved bucket secret name %s" $secret) -}}{{- end }}
{{ include "factory-library.bucketSecret" (dict "root" $ "name" $secret "bucket" $bucket "access" $access) }}
{{- if hasKey . "public" }}
{{ include "factory-library.mirror" (dict "root" $ "bucket" $bucket "base" $base "public" .public) }}
{{- end }}
{{- end }}
{{- end -}}
