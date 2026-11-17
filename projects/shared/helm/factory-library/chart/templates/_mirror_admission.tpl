{{/* All Kubernetes Pod Secret reference paths, including debug containers. */}}
{{- define "factory-library.containerUsesMirrorSecret" -}}
(has(c.envFrom) && c.envFrom.exists(e,
  has(e.secretRef) && e.secretRef.name == {{ . | toJson }})) ||
(has(c.env) && c.env.exists(e,
  has(e.valueFrom) && has(e.valueFrom.secretKeyRef) &&
  e.valueFrom.secretKeyRef.name == {{ . | toJson }}))
{{- end -}}

{{/* Separate policies keep CEL type checking specific to each object shape.
The factory uses per-controller ServiceAccounts (--use-service-account-credentials).
Never trust a Pod serviceAccountName or ownerReference without the API caller. */}}
{{- define "factory-library.mirrorAdmission" -}}
{{- $base := printf "%s-%s" .root.Release.Namespace .secret -}}
{{- $shapes := list
  (dict "suffix" "" "group" "" "resources" (list "pods" "pods/ephemeralcontainers") "podSpec" "object.spec" "creator" "job-controller")
  (dict "suffix" "-jobs" "group" "batch" "resources" (list "jobs") "podSpec" "object.spec.template.spec" "creator" "cronjob-controller")
  (dict "suffix" "-cronjobs" "group" "batch" "resources" (list "cronjobs") "podSpec" "object.spec.jobTemplate.spec.template.spec" "creator" "argocd") -}}
{{- range $shape := $shapes -}}
{{- $policy := printf "%s%s" $base $shape.suffix -}}
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
      - apiGroups: [{{ $shape.group | quote }}]
        apiVersions: [v1]
        operations: [CREATE, UPDATE]
        resources: {{ $shape.resources | toJson }}
  matchConditions:
    - name: mirror-namespace
      expression: request.namespace == {{ $.root.Release.Namespace | toJson }}
  variables:
    - name: podSpec
      expression: {{ $shape.podSpec }}
    - name: usesAccount
      expression: >-
        (has(variables.podSpec.serviceAccountName) && variables.podSpec.serviceAccountName == {{ $.name | toJson }}) ||
        (has(variables.podSpec.serviceAccount) && variables.podSpec.serviceAccount == {{ $.name | toJson }})
    - name: usesSecret
      expression: >-
        (has(variables.podSpec.volumes) && variables.podSpec.volumes.exists(v,
          (has(v.secret) && v.secret.secretName == {{ $.secret | toJson }}) ||
          (has(v.projected) && v.projected.sources.exists(s,
            has(s.secret) && s.secret.name == {{ $.secret | toJson }})))) ||
        (has(variables.podSpec.imagePullSecrets) && variables.podSpec.imagePullSecrets.exists(s,
          s.name == {{ $.secret | toJson }})) ||
        variables.podSpec.containers.exists(c,
          {{- include "factory-library.containerUsesMirrorSecret" $.secret | nindent 10 }}) ||
        (has(variables.podSpec.initContainers) && variables.podSpec.initContainers.exists(c,
          {{- include "factory-library.containerUsesMirrorSecret" $.secret | nindent 10 }})) ||
        (has(variables.podSpec.ephemeralContainers) && variables.podSpec.ephemeralContainers.exists(c,
          {{- include "factory-library.containerUsesMirrorSecret" $.secret | nindent 10 }}))
  validations:
    - expression: >-
        !(variables.usesSecret || variables.usesAccount{{ if eq $shape.creator "argocd" }} || object.metadata.name == {{ $.name | toJson }}{{ end }}) ||
        (request.operation == "UPDATE" && oldObject != null &&
          request.userInfo.username == "system:serviceaccount:kube-system:generic-garbage-collector" &&
          object.spec == oldObject.spec) ||
        (has(variables.podSpec.serviceAccountName) && variables.podSpec.serviceAccountName == {{ $.name | toJson }} &&
        {{- if eq $shape.creator "argocd" }}
          object.metadata.name == {{ $.name | toJson }} &&
          request.userInfo.username == "system:serviceaccount:argocd:argocd-application-controller")
        {{- else }}
          request.userInfo.username in ["system:serviceaccount:kube-system:{{ $shape.creator }}"{{ if eq $shape.creator "cronjob-controller" }}, "system:serviceaccount:kube-system:job-controller"{{ end }}] &&
          has(object.metadata.ownerReferences) && object.metadata.ownerReferences.exists(o,
            o.apiVersion == "batch/v1" && o.kind == "{{ if eq $shape.creator "job-controller" }}Job{{ else }}CronJob{{ end }}" &&
            {{- if eq $shape.creator "cronjob-controller" }}
            o.name == {{ $.name | toJson }} &&
            {{- end }}
            has(o.controller) && o.controller){{ if eq $shape.creator "job-controller" }} &&
          (!has(request.subResource) || request.subResource != "ephemeralcontainers"){{ else }} &&
          (request.operation != "CREATE" || request.userInfo.username == "system:serviceaccount:kube-system:cronjob-controller"){{ end }})
        {{- end }}
      message: Only the mirror controller chain may use its R2 Secret or ServiceAccount.
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
{{- end }}
{{- end -}}
