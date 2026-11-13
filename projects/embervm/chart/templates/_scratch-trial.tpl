{{/* Fixed paths keep the trial outside production's parent mount and image. */}}
{{- define "embervm.scratchTrial.validate" -}}
{{- $v := .Values -}}
{{- $trial := $v.scratchPrep.isolatedDevTrial -}}
{{- if $trial.enabled -}}
{{- if or (ne .Release.Namespace "embervm-dev") (ne .Release.Name "embervm-dev") -}}
{{- fail "isolatedDevTrial requires the embervm-dev release and namespace" -}}
{{- end -}}
{{- if or (not (regexMatch "^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$" $trial.nodeName)) (gt (len $trial.nodeName) 253) -}}
{{- fail "isolatedDevTrial requires one explicit nodeName, without wildcards" -}}
{{- end -}}
{{- if not (regexMatch "^[1-8]$" (toString $v.scratchSizeGi)) -}}
{{- fail "isolatedDevTrial requires explicit scratchSizeGi of 1 through 8 GiB after the named-node capacity audit" -}}
{{- end -}}
{{- if or (not $v.scratchPrep.enabled) (ne $v.scratchPrep.filesystem "xfs") $v.noded.enabled (not $v.bricks.enabled) -}}
{{- fail "isolatedDevTrial requires XFS scratchPrep and bricks, with the noded DaemonSet disabled" -}}
{{- end -}}
{{- if or $v.noded.firecracker.sharedScratch.enabled (ne $v.noded.firecracker.nvmeRoot "/var/lib/embervm-xfs-trial/scratch") (ne $v.noded.firecracker.nvmeRootHostPathType "Directory") -}}
{{- fail "isolatedDevTrial requires its isolated nvmeRoot, Directory hostPath and sharedScratch disabled" -}}
{{- end -}}
{{- if or (ne (index $v.noded.nodeSelector "cloud.google.com/gke-nodepool") "ember-bricks") (ne (index $v.noded.nodeSelector "homelab.io/firecracker" | toString) "true") (gt (len $v.bricks.nodeFloors) 0) $v.bricks.autoscale.archiveAckGate.enabled -}}
{{- fail "isolatedDevTrial requires the ember-bricks Firecracker selector, no nodeFloors and archiveAckGate disabled" -}}
{{- end -}}
{{- if or $v.noded.store.credentials.enabled $v.noded.store.endpoint $v.noded.store.bucket $v.noded.bearerTokenSecret.enabled $v.kekRoot.enabled $v.tokenBroker.enabled $v.egress.enabled $v.opLog.postgres.enabled -}}
{{- fail "isolatedDevTrial requires local-only dev storage with store, bearer token, KEK, broker, egress and Postgres credentials disabled" -}}
{{- end -}}
{{- range $name, $workload := $v.workloads -}}
{{- if not (regexMatch "^/var/lib/embervm-xfs-trial/scratch/([a-zA-Z0-9_-]+/)+[a-zA-Z0-9_-]+[.]ext4$" $workload.rootfsPath) -}}
{{- fail (printf "isolatedDevTrial requires workloads.%s.rootfsPath inside its isolated scratch" $name) -}}
{{- end -}}
{{- end -}}
{{- if ne $v.rbac.scope "namespace" -}}
{{- fail "isolatedDevTrial requires namespace RBAC and release-derived identities" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "embervm.scratchTrial.affinity" -}}
nodeAffinity:
  requiredDuringSchedulingIgnoredDuringExecution:
    nodeSelectorTerms:
      - matchFields:
          - key: metadata.name
            operator: In
            values:
              - {{ .Values.scratchPrep.isolatedDevTrial.nodeName | quote }}
{{- end -}}
