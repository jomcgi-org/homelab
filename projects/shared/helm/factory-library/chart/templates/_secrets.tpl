{{/* Generate inside the cluster. Offline Helm/Argo renders never mint or rotate
credentials. The kept Secret omits data so server-side apply leaves runtime
fields alone. The initializer's optimistic patch preserves existing keys. */}}
{{- define "factory-library.secretGenerator" -}}
import base64
import json
import os
import secrets
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path

account = Path("/var/run/secrets/kubernetes.io/serviceaccount")
context = ssl.create_default_context(cafile=str(account / "ca.crt"))
host = os.environ["KUBERNETES_SERVICE_HOST"]
if ":" in host:
    host = f"[{host}]"
port = os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443")
url = f"https://{host}:{port}/api/v1/namespaces/{os.environ['POD_NAMESPACE']}/secrets/{os.environ['SECRET_NAME']}"
keys = json.loads(os.environ["SECRET_KEYS"])
for attempt in range(5):
    headers = {"Authorization": "Bearer " + (account / "token").read_text().strip()}
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), context=context, timeout=30) as response:
        current = json.load(response)
    data = current.get("data") or {}
    missing = set(keys) - data.keys()
    if not missing:
        break
    for key in missing:
        data[key] = base64.b64encode(secrets.token_urlsafe(int(keys[key])).encode()).decode()
    patch = [
        {"op": "test", "path": "/metadata/resourceVersion", "value": current["metadata"]["resourceVersion"]},
        {"op": "add", "path": "/data", "value": data},
    ]
    headers["Content-Type"] = "application/json-patch+json"
    try:
        request = urllib.request.Request(url, data=json.dumps(patch).encode(), headers=headers, method="PATCH")
        with urllib.request.urlopen(request, context=context, timeout=30):
            pass
        break
    except urllib.error.HTTPError as error:
        if error.code not in (409, 422) or attempt == 4:
            raise
        time.sleep(1)
{{- end -}}

{{- define "factory-library.secrets" -}}
{{- range $name, $secret := .Values.secrets -}}
{{- include "factory-library.name" $name -}}
{{- include "factory-library.keys" (dict "path" (printf "secret %s" $name) "value" $secret "allowed" (list "workload" "onePassword" "generated")) -}}
{{- $owner := required "factory-library: secret.workload is required" $secret.workload -}}
{{- if not (hasKey $.Values.workloads $owner) -}}{{- fail (printf "factory-library: secret %s names unknown workload %s" $name $owner) -}}{{- end -}}
{{- if eq (hasKey $secret "onePassword") (hasKey $secret "generated") -}}
{{- fail "factory-library: a secret requires exactly one of onePassword or generated; literal values are not supported" -}}
{{- end -}}
{{- $ctx := dict "root" $ "name" $owner -}}
{{- if hasKey $secret "onePassword" -}}
{{- $op := $secret.onePassword -}}
{{- include "factory-library.keys" (dict "path" "onePassword" "value" $op "allowed" (list "itemPath" "type")) -}}
{{- $path := required "factory-library: onePassword.itemPath is required" $op.itemPath -}}
{{- if not (regexMatch "^vaults/[^/]+/items/[^/]+$" $path) -}}{{- fail "factory-library: onePassword.itemPath must be vaults/<vault>/items/<item>" -}}{{- end }}
---
apiVersion: onepassword.com/v1
kind: OnePasswordItem
type: {{ $op.type | default "Opaque" | quote }}
metadata:
  name: {{ $name }}
  namespace: {{ $.Release.Namespace }}
  annotations:
    argocd.argoproj.io/sync-wave: "-2"
    {{- with (include "factory-library.annotations" $ctx | fromYaml).annotations }}
    {{- toYaml . | nindent 4 }}
    {{- end }}
spec:
  itemPath: {{ $path }}
{{- else -}}
{{- $generated := $secret.generated -}}
{{- include "factory-library.keys" (dict "path" "generated secret" "value" $generated "allowed" (list "keys" "image" "resources")) -}}
{{- $keys := required "factory-library: generated.keys is required" $generated.keys -}}
{{- range $key, $bytes := $keys -}}
{{- if not (regexMatch "^[a-zA-Z0-9._-]+$" $key) -}}{{- fail "factory-library: invalid generated Secret key" -}}{{- end -}}
{{- if or (not (regexMatch "^[0-9]+$" (toString $bytes))) (lt (int $bytes) 16) (gt (int $bytes) 128) -}}
{{- fail "factory-library: generated key entropy must be 16 to 128 bytes" -}}
{{- end -}}
{{- end -}}
{{- $initName := printf "%s-generate" $name -}}
{{- if hasKey $.Values.workloads $initName -}}{{- fail (printf "factory-library: reserved generated workload name %s" $initName) -}}{{- end }}
---
apiVersion: v1
kind: Secret
metadata:
  name: {{ $name }}
  annotations:
    argocd.argoproj.io/sync-options: Prune=false,Delete=false
    helm.sh/resource-policy: keep
    argocd.argoproj.io/sync-wave: "-2"
    {{- with (include "factory-library.annotations" $ctx | fromYaml).annotations }}
    {{- toYaml . | nindent 4 }}
    {{- end }}
type: Opaque
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: {{ $initName }}
  annotations:
    argocd.argoproj.io/sync-wave: "-2"
rules:
  - apiGroups: [""]
    resources: [secrets]
    resourceNames: [{{ $name | quote }}]
    verbs: [get, patch]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: {{ $initName }}
  annotations:
    argocd.argoproj.io/sync-wave: "-2"
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: {{ $initName }}
subjects:
  - kind: ServiceAccount
    name: {{ $initName }}
    namespace: {{ $.Release.Namespace }}
{{- $init := dict "kind" "Job" "kubernetesApi" true "image" (required "factory-library: generated.image with Python 3 is required" $generated.image) "resources" $generated.resources "command" (list "python3" "-c" (include "factory-library.secretGenerator" .)) "env" (list (dict "name" "POD_NAMESPACE" "value" $.Release.Namespace) (dict "name" "SECRET_NAME" "value" $name) (dict "name" "SECRET_KEYS" "value" (toJson $keys))) "network" (dict "egress" (list (dict "peer" "kube-apiserver" "port" 6443))) -}}
{{ include "factory-library.job" (dict "root" $ "name" $initName "workload" $init "secretInitializer" true) }}
{{- end }}
{{- end }}
{{- end -}}
