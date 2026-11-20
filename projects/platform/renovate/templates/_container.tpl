{{- define "renovate.container" -}}
{{- $githubApp := .Values.githubApp | default dict -}}
image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"
imagePullPolicy: {{ .Values.image.pullPolicy }}
{{- if $githubApp.enabled }}
# Preserve the image entrypoint; its command loads the per-run credentials.
args:
  - /bin/sh
  - -c
  - |
    set -eu
    RENOVATE_TOKEN="$(cat /var/run/renovate-token/token)"
    RENOVATE_GIT_AUTHOR="$(cat /var/run/renovate-token/git-author)"
    test -n "$RENOVATE_TOKEN"
    test -n "$RENOVATE_GIT_AUTHOR"
    export RENOVATE_TOKEN RENOVATE_GIT_AUTHOR
    exec renovate
{{- end }}
env:
  {{- if $githubApp.enabled }}
  - name: RENOVATE_USERNAME
    value: {{ $githubApp.username | quote }}
  - name: RENOVATE_PLATFORM_COMMIT
    value: enabled
  {{- else }}
  - name: RENOVATE_TOKEN
    valueFrom:
      secretKeyRef:
        name: {{ .Values.onepassword.secretName }}
        key: {{ .Values.onepassword.tokenKey }}
  {{- end }}
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
    value: '["^bazel/tools/format/update-python-requirements\\.sh$","^projects/platform-factory/substrate/render-upstream\\.sh --refresh$"]'
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
  {{- if $githubApp.enabled }}
  - name: github-token
    mountPath: /var/run/renovate-token
    readOnly: true
  {{- end }}
  - name: workspace
    mountPath: /tmp
{{- end }}
