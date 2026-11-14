{{- define "renovate.container" -}}
image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"
imagePullPolicy: {{ .Values.image.pullPolicy }}
env:
  - name: RENOVATE_TOKEN
    valueFrom:
      secretKeyRef:
        name: {{ .Values.onepassword.secretName }}
        key: {{ .Values.onepassword.tokenKey }}
  - name: RENOVATE_PLATFORM
    value: github
  - name: RENOVATE_REPOSITORIES
    value: jomcgi-org/homelab
  - name: RENOVATE_AUTODISCOVER
    value: "false"
  - name: RENOVATE_ONBOARDING
    value: "false"
  - name: RENOVATE_REQUIRE_CONFIG
    value: required
  - name: RENOVATE_ALLOW_SCRIPTS
    value: "false"
  # uv lock can run build backends even with lifecycle scripts off.
  - name: RENOVATE_CUSTOM_ENV_VARIABLES
    value: '{"UV_NO_BUILD":"true"}'
  - name: RENOVATE_ALLOW_PLUGINS
    value: "false"
  - name: RENOVATE_ALLOWED_COMMANDS
    value: '["^bazel/tools/format/update-python-requirements\\.sh$"]'
  - name: RENOVATE_ALLOWED_UNSAFE_EXECUTIONS
    value: '["bazelModDeps"]'
  - name: RENOVATE_BASE_DIR
    value: /tmp/renovate
  - name: RENOVATE_CONTAINERBASE_DIR
    value: /tmp/containerbase
  - name: HOME
    value: /tmp/renovate/home
  - name: LOG_FORMAT
    value: json
  - name: LOG_LEVEL
    value: info
resources:
  {{- toYaml .Values.resources | nindent 2 }}
securityContext:
  allowPrivilegeEscalation: false
  capabilities:
    drop:
      - ALL
  readOnlyRootFilesystem: true
  runAsNonRoot: true
volumeMounts:
  - name: workspace
    mountPath: /tmp
{{- end }}
