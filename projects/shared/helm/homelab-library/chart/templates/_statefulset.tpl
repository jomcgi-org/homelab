{{/*
StatefulSet resource.
Renders a complete StatefulSet for a named component with persistent storage.
All config is read from .Values.<component> by convention.

Usage:
  {{- include "homelab.statefulset" (dict "context" . "component" "api") }}
  {{- include "homelab.statefulset" (dict "context" . "component" "db" "componentName" "database") }}

Required values under .<component>:
  image.repository, image.tag, image.pullPolicy
  image.digest — optional, but preferred when present: see homelab.imageRef in
                 _helpers.tpl for why the digest and not the tag is deployed.
  persistence.size, persistence.mountPath

Optional values (with defaults):
  enabled (true), replicas (1), containerPort (8080), portName ("http"),
  args ([]),
  probes.liveness.path ("/health"), probes.readiness.path ("/health"),
  probes.liveness.exec ([]) — if set, uses exec probe instead of httpGet,
  probes.readiness.exec ([]) — same for readiness,
  env ([]), resources ({}), volumes ([]), volumeMounts ([]),
  podAnnotations ({}), podSecurityContext (falls back to global),
  securityContext (falls back to global),
  persistence.storageClassName (unset — uses cluster default),
  persistence.volumeName ("data")

Optional dict keys:
  componentName — override the name used in metadata/labels/container
                  (defaults to component); useful when the values key is
                  camelCase but the Kubernetes resource name should be
                  kebab-case.
*/}}
{{- define "homelab.statefulset" -}}
{{- $ctx := .context -}}
{{- $component := .component -}}
{{- $name := default $component .componentName -}}
{{- $vals := index $ctx.Values $component -}}
{{- $volName := dig "persistence" "volumeName" "data" $vals -}}
{{- if (default true $vals.enabled) }}
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: {{ include "homelab.fullname" $ctx }}-{{ $name }}
  labels:
    {{- include "homelab.componentLabels" (dict "context" $ctx "component" $name) | nindent 4 }}
spec:
  serviceName: {{ include "homelab.fullname" $ctx }}-{{ $name }}
  replicas: {{ $vals.replicas | default 1 }}
  selector:
    matchLabels:
      {{- include "homelab.componentSelectorLabels" (dict "context" $ctx "component" $name) | nindent 6 }}
  template:
    metadata:
      labels:
        {{- include "homelab.componentSelectorLabels" (dict "context" $ctx "component" $name) | nindent 8 }}
      {{- with $vals.podAnnotations }}
      annotations:
        {{- toYaml . | nindent 8 }}
      {{- end }}
    spec:
      {{- if $ctx.Values.imagePullSecret.enabled }}
      imagePullSecrets:
        - name: ghcr-imagepull-secret
      {{- else if $ctx.Values.imagePullSecrets }}
      imagePullSecrets:
        {{- toYaml $ctx.Values.imagePullSecrets | nindent 8 }}
      {{- end }}
      serviceAccountName: {{ include "homelab.serviceAccountName" $ctx }}
      securityContext:
        {{- $podSec := default $ctx.Values.podSecurityContext $vals.podSecurityContext -}}
        {{- toYaml $podSec | nindent 8 }}
      containers:
        - name: {{ $name }}
          image: "{{ include "homelab.imageRef" $vals.image }}"
          imagePullPolicy: {{ $vals.image.pullPolicy }}
          securityContext:
            {{- $sec := default $ctx.Values.securityContext $vals.securityContext -}}
            {{- toYaml $sec | nindent 12 }}
          {{- with $vals.args }}
          args:
            {{- toYaml . | nindent 12 }}
          {{- end }}
          ports:
            - name: {{ $vals.portName | default "http" }}
              containerPort: {{ $vals.containerPort | default 8080 }}
              protocol: TCP
          {{- with $vals.env }}
          env:
            {{- toYaml . | nindent 12 }}
          {{- end }}
          livenessProbe:
            {{- if (dig "probes" "liveness" "exec" nil $vals) }}
            exec:
              command:
                {{- toYaml (dig "probes" "liveness" "exec" (list) $vals) | nindent 16 }}
            {{- else }}
            httpGet:
              path: {{ dig "probes" "liveness" "path" "/health" $vals }}
              port: {{ $vals.portName | default "http" }}
            {{- end }}
            initialDelaySeconds: {{ dig "probes" "liveness" "initialDelaySeconds" 10 $vals }}
            periodSeconds: {{ dig "probes" "liveness" "periodSeconds" 10 $vals }}
            timeoutSeconds: {{ dig "probes" "liveness" "timeoutSeconds" 1 $vals }}
            failureThreshold: {{ dig "probes" "liveness" "failureThreshold" 3 $vals }}
          readinessProbe:
            {{- if (dig "probes" "readiness" "exec" nil $vals) }}
            exec:
              command:
                {{- toYaml (dig "probes" "readiness" "exec" (list) $vals) | nindent 16 }}
            {{- else }}
            httpGet:
              path: {{ dig "probes" "readiness" "path" "/health" $vals }}
              port: {{ $vals.portName | default "http" }}
            {{- end }}
            initialDelaySeconds: {{ dig "probes" "readiness" "initialDelaySeconds" 5 $vals }}
            periodSeconds: {{ dig "probes" "readiness" "periodSeconds" 5 $vals }}
            timeoutSeconds: {{ dig "probes" "readiness" "timeoutSeconds" 1 $vals }}
            failureThreshold: {{ dig "probes" "readiness" "failureThreshold" 3 $vals }}
          {{- with $vals.resources }}
          resources:
            {{- toYaml . | nindent 12 }}
          {{- end }}
          volumeMounts:
            - name: tmp
              mountPath: /tmp
            - name: {{ $volName }}
              mountPath: {{ $vals.persistence.mountPath }}
            {{- with $vals.volumeMounts }}
            {{- toYaml . | nindent 12 }}
            {{- end }}
      volumes:
        - name: tmp
          emptyDir: {}
        {{- with $vals.volumes }}
        {{- toYaml . | nindent 8 }}
        {{- end }}
      {{- with $ctx.Values.nodeSelector }}
      nodeSelector:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- with $ctx.Values.affinity }}
      affinity:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- with $ctx.Values.tolerations }}
      tolerations:
        {{- toYaml . | nindent 8 }}
      {{- end }}
  volumeClaimTemplates:
    - metadata:
        name: {{ $volName }}
      spec:
        accessModes:
          - ReadWriteOnce
        {{- with $vals.persistence.storageClassName }}
        storageClassName: {{ . }}
        {{- end }}
        resources:
          requests:
            storage: {{ $vals.persistence.size }}
{{- end }}
{{- end }}
