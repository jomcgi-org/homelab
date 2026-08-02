{{- define "embervm.name" -}}
{{- default "embervm" .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "embervm.fullname" -}}
{{- printf "%s-%s" .Release.Name (include "embervm.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "embervm.labels" -}}
app.kubernetes.io/name: {{ include "embervm.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "embervm.selectorLabels" -}}
app.kubernetes.io/name: {{ include "embervm.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "embervm.serviceAccountName" -}}
{{- include "embervm.fullname" . -}}
{{- end -}}

{{- define "embervm.tokenBroker.name" -}}{{ printf "%s-tokenbroker" (include "embervm.name" .) | trunc 63 | trimSuffix "-" }}{{- end -}}
{{- define "embervm.tokenBroker.fullname" -}}{{ printf "%s-tokenbroker" (include "embervm.fullname" .) | trunc 63 | trimSuffix "-" }}{{- end -}}
{{- define "embervm.tokenBroker.selectorLabels" -}}app.kubernetes.io/name: {{ include "embervm.tokenBroker.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: tokenbroker{{- end -}}
{{- define "embervm.tokenBroker.labels" -}}{{ include "embervm.tokenBroker.selectorLabels" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}{{- end -}}
{{- define "embervm.tokenBroker.serviceAccountName" -}}{{ include "embervm.tokenBroker.fullname" . }}{{- end -}}

{{/*
The node daemon (embervm-noded) is a SECOND Deployment in this one chart/release.
It uses a DISTINCT app.kubernetes.io/name ("<name>-noded") so its selector is
disjoint from the control plane's: the control-plane Deployment's selector is
already live and immutable, so it cannot gain a component label without a
delete+recreate. The noded selector additionally carries a component label to
disambiguate self-documentingly (and satisfy the selector-component lint).
*/}}
{{- define "embervm.noded.name" -}}
{{- printf "%s-noded" (include "embervm.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "embervm.noded.fullname" -}}
{{- printf "%s-noded" (include "embervm.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "embervm.noded.selectorLabels" -}}
app.kubernetes.io/name: {{ include "embervm.noded.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: noded
{{- end -}}

{{- define "embervm.noded.labels" -}}
{{ include "embervm.noded.selectorLabels" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "embervm.noded.serviceAccountName" -}}
{{- include "embervm.noded.fullname" . -}}
{{- end -}}

{{/*
Size-class BRICK labels (brick-capacity, ADR embervm/013). A brick is a noded
Deployment, so it SHARES noded's app.kubernetes.io/name (the pods are noded pods)
but carries a DISTINCT component ("noded-brick") plus its size-class label. That
keeps every brick class's selector disjoint from the DaemonSet's (component=noded)
and from every other class, so no two controllers ever fight over a pod. Input
dict: (dict "ctx" $ "class" $class.name).
*/}}
{{- define "embervm.brick.selectorLabels" -}}
app.kubernetes.io/name: {{ include "embervm.noded.name" .ctx }}
app.kubernetes.io/instance: {{ .ctx.Release.Name }}
app.kubernetes.io/component: noded-brick
embervm.jomcgi.dev/size-class: {{ .class | quote }}
{{- end -}}

{{- define "embervm.brick.labels" -}}
{{ include "embervm.brick.selectorLabels" (dict "ctx" .ctx "class" .class) }}
app.kubernetes.io/managed-by: {{ .ctx.Release.Service }}
{{- end -}}

{{/*
Per-node brick FLOOR labels (brick-capacity PR-3). A floor Deployment is still
"a brick of this class" (same component + size-class labels as the class-wide
Deployment, on purpose: it is the same kind of pod), but it MUST NOT share the
class Deployment's selector - two Deployments' ReplicaSet controllers fighting
over the same pods is a live bug, not a style choice. The extra
embervm.jomcgi.dev/brick-floor=<node> label makes the floor's selector disjoint
from its class's (and from every other floor's) while still being reachable by
the shared size-class label for anything that wants "all 2gi bricks, floor or
not". Input dict: (dict "ctx" $ "class" $class "node" $floor.node).
*/}}
{{- define "embervm.brickFloor.selectorLabels" -}}
{{ include "embervm.brick.selectorLabels" (dict "ctx" .ctx "class" .class) }}
embervm.jomcgi.dev/brick-floor: {{ .node | quote }}
{{- end -}}

{{- define "embervm.brickFloor.labels" -}}
{{ include "embervm.brickFloor.selectorLabels" (dict "ctx" .ctx "class" .class "node" .node) }}
app.kubernetes.io/managed-by: {{ .ctx.Release.Service }}
{{- end -}}

{{/*
The per-node serving Envoy tier (R3, PR-3) is a DaemonSet in this one chart. Like
noded it uses a DISTINCT app.kubernetes.io/name ("<name>-serving-envoy") so its
selector is disjoint from the control plane and noded, and carries a component
label to disambiguate self-documentingly.
*/}}
{{- define "embervm.servingEnvoy.name" -}}
{{- printf "%s-serving-envoy" (include "embervm.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "embervm.servingEnvoy.fullname" -}}
{{- printf "%s-serving-envoy" (include "embervm.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "embervm.servingEnvoy.selectorLabels" -}}
app.kubernetes.io/name: {{ include "embervm.servingEnvoy.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: serving-envoy
{{- end -}}

{{- define "embervm.servingEnvoy.labels" -}}
{{ include "embervm.servingEnvoy.selectorLabels" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/*
Node-provisioning contract actuator (ADR embervm/012 storage-tiers amendment):
the scratch-prep DaemonSet is a THIRD workload in this chart. Like noded and the
serving Envoy it uses a DISTINCT app.kubernetes.io/name ("<name>-scratch-prep")
so its selector is disjoint from every other workload's, and carries a component
label to disambiguate self-documentingly.
*/}}
{{- define "embervm.scratchPrep.name" -}}
{{- printf "%s-scratch-prep" (include "embervm.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "embervm.scratchPrep.fullname" -}}
{{- printf "%s-scratch-prep" (include "embervm.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "embervm.scratchPrep.selectorLabels" -}}
app.kubernetes.io/name: {{ include "embervm.scratchPrep.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: scratch-prep
{{- end -}}

{{- define "embervm.scratchPrep.labels" -}}
{{ include "embervm.scratchPrep.selectorLabels" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/*
The stable per-node serving address the edge Envoy Gateway sees. v1 is one node
so a ClusterIP Service suffices; the name is release-derived like every other
service name here (survives a release rename).
*/}}
{{- define "embervm.serving.fullname" -}}
{{- printf "%s-serving" (include "embervm.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Per-guest-digest rootfs path: <dir>/rootfs-<digest>.ext4, so each pinned guest-image
version bakes its OWN read-only rootfs file instead of overwriting one fixed path.
The Firecracker memfile embeds this host path (restore re-attaches it, never
re-issuing PutDrive), so a chart roll that rebuilt the fixed file in place used to
swap the bytes under every banked session snapshot -> EXT4 corruption on restore.
Digest-naming makes the artifact immutable per version; old files are reaped by the
noded rootfs GC (#4088) once no registry ref and no READY base points at them.

The suffix is the DIGEST, never the tag, and that distinction is load-bearing.
Bazel stamps every build with a fresh <timestamp>-<commit> tag even when the image
content is byte-identical, so a tag-derived name changed on EVERY deploy. That name
is BASE_ROOTFS_PATH in the shared noded pod spec, so it rolled all brick
Deployments on every chart bump, killing live VMs mid-flight for a rebuild that
produced identical bytes (issue #4147: 11 brick rolls in a day against 1 real image
change). The digest moves only when the guest image actually moves, which is the
property this path always meant to express.

Input: (dict "wl" $wl "top" $top). MUST be used by BOTH the rootfs-builder
BASE_ROOTFS_PATH and the EMBERVM_NODED_IMAGES rootfsPath so the built file and the
path noded attaches are byte-identical.
*/}}
{{- define "embervm.noded.rootfsPath" -}}
{{- $digest := required "guestImage.digest is required to name a base rootfs (Bazel pins it via helm_chart images=). An empty suffix would collide across guest versions and swap bytes under banked snapshots." .top.guestImage.digest -}}
{{- $suffix := $digest | toString | replace ":" "-" | replace "@" "-" | replace "/" "-" -}}
{{- printf "%s-%s.ext4" (trimSuffix ".ext4" .wl.rootfsPath) $suffix -}}
{{- end -}}
