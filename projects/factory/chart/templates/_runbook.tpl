{{- define "runbook.ownerContainer" -}}
- name: runbook-owner
  image: {{ include "factory.connectorImage" .root | quote }}
  args: ["runbook-kagent-revert"]
  env:
    - {name: RUNBOOK_OWNER, value: {{ .owner }}}
    - {name: KAGENT_AUTHORITY, value: {{ .root.Values.kagent.config.server.auth.serviceAccount.authority | quote }}}
  securityContext:
    allowPrivilegeEscalation: false
    readOnlyRootFilesystem: true
    capabilities: {drop: ["ALL"]}
  volumeMounts:
    - {name: kagent-token, mountPath: /var/run/secrets/kagent, readOnly: true}
    - {name: ate, mountPath: /var/run/ate, readOnly: true}
    - {name: runbook-api, mountPath: /var/run/secrets/kubernetes.io/serviceaccount, readOnly: true}
{{- end }}
{{- define "runbook.ownerVolumes" -}}
- name: ate
  projected:
    defaultMode: 0440
    sources:
      - clusterTrustBundle:
          signerName: servicedns.podcert.ate.dev/identity
          labelSelector:
            matchLabels:
              podcert.ate.dev/canarying: live
          path: ca.pem
      - podCertificate:
          signerName: postgres.podcert.ate.dev/identity
          keyType: ECDSAP256
          credentialBundlePath: client.pem
          userAnnotations:
            postgres.podcert.ate.dev/username: factory_reaper
- name: runbook-api
  projected:
    sources:
      - serviceAccountToken: {path: token, expirationSeconds: 3600}
      - configMap:
          name: kube-root-ca.crt
          items: [{key: ca.crt, path: ca.crt}]
{{- end }}
