{{/* All Kubernetes Pod Secret reference paths, including debug containers. */}}
{{- define "factory-library.containerUsesMirrorSecret" -}}
(has(c.envFrom) && c.envFrom.exists(e,
  has(e.secretRef) && e.secretRef.name == {{ . | toJson }})) ||
(has(c.env) && c.env.exists(e,
  has(e.valueFrom) && has(e.valueFrom.secretKeyRef) &&
  e.valueFrom.secretKeyRef.name == {{ . | toJson }}))
{{- end -}}

{{- define "factory-library.mirrorAdmission" -}}
{{- $policy := printf "%s-%s" .root.Release.Namespace .secret -}}
{{- include "factory-library.name" $policy }}
---
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingAdmissionPolicy
metadata:
  name: {{ $policy }}
  annotations:
    argocd.argoproj.io/sync-wave: "-3"
    argocd.argoproj.io/sync-options: Prune=false,Delete=false
    helm.sh/resource-policy: keep
spec:
  failurePolicy: Fail
  matchConstraints:
    resourceRules:
      - apiGroups: [""]
        apiVersions: [v1]
        operations: [CREATE, UPDATE]
        resources: [pods, pods/ephemeralcontainers]
  matchConditions:
    - name: mirror-namespace
      expression: request.namespace == {{ .root.Release.Namespace | toJson }}
  variables:
    - name: usesSecret
      expression: >-
        (has(object.spec.volumes) && object.spec.volumes.exists(v,
          (has(v.secret) && v.secret.secretName == {{ .secret | toJson }}) ||
          (has(v.projected) && v.projected.sources.exists(s,
            has(s.secret) && s.secret.name == {{ .secret | toJson }})))) ||
        (has(object.spec.imagePullSecrets) && object.spec.imagePullSecrets.exists(s,
          s.name == {{ .secret | toJson }})) ||
        object.spec.containers.exists(c,
          {{- include "factory-library.containerUsesMirrorSecret" .secret | nindent 10 }}) ||
        (has(object.spec.initContainers) && object.spec.initContainers.exists(c,
          {{- include "factory-library.containerUsesMirrorSecret" .secret | nindent 10 }})) ||
        (has(object.spec.ephemeralContainers) && object.spec.ephemeralContainers.exists(c,
          {{- include "factory-library.containerUsesMirrorSecret" .secret | nindent 10 }}))
  validations:
    - expression: >-
        !variables.usesSecret ||
        (has(object.spec.serviceAccountName) && object.spec.serviceAccountName == {{ .name | toJson }})
      message: Only this mirror ServiceAccount may use its R2 Secret.
---
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingAdmissionPolicyBinding
metadata:
  name: {{ $policy }}
  annotations:
    argocd.argoproj.io/sync-wave: "-3"
    argocd.argoproj.io/sync-options: Prune=false,Delete=false
    helm.sh/resource-policy: keep
spec:
  policyName: {{ $policy }}
  validationActions: [Deny]
{{- end -}}
