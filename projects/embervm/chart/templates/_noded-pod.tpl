{{/*
Shared noded pod spec: the body of a noded pod's `template.spec`, factored out so
the interim DaemonSet (noded-deployment.yaml) and the size-class brick Deployments
(brick-deployment.yaml) render one identical pod from one source. The workload
KIND, selector, and update strategy differ per caller and stay in the per-workload
templates; everything a noded pod actually IS (securityContext, KVM + nvme host
mounts, rootfs-builder initContainers, the full daemon env, probes) lives here.

Input dict:
  * ctx          - the chart root context (`.` / `$`), for `.Values` / `.Release`.
  * sizeClass    - the brick T-shirt class label ("2gi".."16gi"); "" for the legacy
                   DaemonSet, which renders NO EMBERVM_NODED_SIZE_CLASS env (the
                   wildcard class the control plane treats as matching every
                   request), so the DS pod is byte-identical to its pre-brick form.
  * resources    - the noded container's resources map. The DaemonSet passes
                   .Values.noded.resources unchanged; a brick passes its class's
                   own request/limit block (memory req==limit, cpu request only).
  * nodeSelector - OPTIONAL override for the pod's nodeSelector map. Absent (or
                   nil) falls back to $ctx.Values.noded.nodeSelector (the
                   fleet-wide FC-node label every bin-packed brick uses). A
                   per-node floor Deployment (brick-capacity PR-3) passes
                   {kubernetes.io/hostname: <node>} here to pin onto one host,
                   which is why this is a dict field and not just $ctx.Values.

Call with `{{- include "embervm.noded.podSpec" (dict "ctx" . "sizeClass" "" "resources" .Values.noded.resources) | nindent 6 }}`.
*/}}
{{- define "embervm.noded.podSpec" -}}
{{- $ctx := .ctx -}}
{{- $nodeSelector := .nodeSelector | default $ctx.Values.noded.nodeSelector -}}
# Safe-rollout drain: give the daemon time to finish in-flight Assigns on
# SIGTERM before Kubernetes SIGKILLs it. Set above the daemon's own drain
# budget (EMBERVM_NODED_DRAIN_TIMEOUT below) so grace always outlasts drain,
# mirroring fc-invoke (grace = drain + 30s).
terminationGracePeriodSeconds: {{ add $ctx.Values.noded.drain.timeoutSeconds 30 }}
serviceAccountName: {{ include "embervm.noded.serviceAccountName" $ctx }}
{{- if $ctx.Values.imagePullSecret.enabled }}
imagePullSecrets:
  - name: {{ $ctx.Values.imagePullSecret.name }}
{{- end }}
priorityClassName: {{ $ctx.Values.noded.priorityClassName }}
nodeSelector:
  {{- toYaml $nodeSelector | nindent 2 }}
{{- with $ctx.Values.noded.tolerations }}
tolerations:
  {{- toYaml . | nindent 2 }}
{{- end }}
# Driving Firecracker needs root + the host KVM device and the nvme scratch
# dir (the firecracker binary + guest kernel are baked into the image at
# /opt/fc). Privileged is required for the per-instance vsock mount namespaces.
securityContext:
  runAsUser: 0
  runAsGroup: 0
{{- if $ctx.Values.workloads }}
# Build each workload's base rootfs in-cluster from its pinned guest image
# (crane export + mkfs.ext4 onto the nvme scratch), so node-4 never needs a
# manual sudo rootfs placement. One builder per workload that declares a
# top-level `<name>.guestImage` block (Bazel pins its repository@digest from the
# guest image's .info provider); the builder bakes that guest's filesystem
# into the workload's rootfsPath. Idempotent (a marker skips the multi-GB
# rebuild when the guest ref is unchanged). Mirrors the fc-invoke pattern.
initContainers:
  {{- range $name, $wl := $ctx.Values.workloads }}
  {{- $top := index $ctx.Values $name }}
  {{- if and $top $top.guestImage $top.guestImage.repository }}
  # kebabcase the workload key: it doubles as this initContainer's name, an
  # RFC 1123 label that must be lowercase (the camelCase key runtimePython
  # would render build-runtimePython-rootfs, which the apiserver rejects).
  - name: build-{{ $name | kebabcase }}-rootfs
    image: "{{ $ctx.Values.rootfsBuilder.image.repository }}@{{ $ctx.Values.rootfsBuilder.image.digest }}"
    command: ["/bin/bash", "/scripts/build-base-rootfs.sh"]
    env:
      - name: GUEST_IMAGE
        value: "{{ $top.guestImage.repository }}@{{ $top.guestImage.digest }}"
      - name: BASE_ROOTFS_PATH
        value: {{ include "embervm.noded.rootfsPath" (dict "wl" $wl "top" $top) | quote }}
      - name: ROOTFS_SIZE
        value: {{ $ctx.Values.rootfsBuilder.rootfsSize | quote }}
      - name: EMBERVM_ROOTFS_RECLAIM_ENABLED
        value: {{ $ctx.Values.rootfsReclaim.enabled | quote }}
      - name: EMBERVM_ROOTFS_RECLAIM_SNAPSHOTS_ROOT
        value: {{ printf "%s/embervm-noded/snapshots" $ctx.Values.noded.firecracker.nvmeRoot | quote }}
      - name: EMBERVM_ROOTFS_RECLAIM_TARGET_FREE_BYTES
        value: {{ $ctx.Values.rootfsReclaim.targetFreeBytes | quote }}
      {{- if $ctx.Values.imagePullSecret.enabled }}
      - name: DOCKER_CONFIG
        value: /ghcr
      {{- end }}
    volumeMounts:
      - name: nvme
        mountPath: {{ $ctx.Values.noded.firecracker.nvmeRoot }}
      - name: rootfs-builder-script
        mountPath: /scripts
        readOnly: true
      - name: rootfs-builder-work
        mountPath: /work
      {{- if $ctx.Values.imagePullSecret.enabled }}
      - name: ghcr-creds
        mountPath: /ghcr
        readOnly: true
      {{- end }}
    resources:
      requests:
        cpu: 500m
        memory: 512Mi
      limits:
        memory: 512Mi
  {{- end }}
  {{- end }}
{{- end }}
containers:
  # This is a defined template PARTIAL, not a standalone manifest, so the
  # manifest-shape k8s rules mis-parse the fragment: the noded container DOES have
  # a readinessProbe (below) and resources (rendered from .resources via toYaml,
  # which the rule cannot see as literal limits). The rendered pod is linted for
  # real at the two call sites (noded-deployment.yaml, brick-deployment.yaml).
  # nosemgrep: require-readiness-probe, require-resource-limits
  - name: noded
    image: "{{ $ctx.Values.noded.image.repository }}@{{ $ctx.Values.noded.image.digest }}"
    imagePullPolicy: {{ $ctx.Values.noded.image.pullPolicy }}
    securityContext:
      privileged: true
    ports:
      - name: grpc
        containerPort: {{ $ctx.Values.noded.grpcPort }}
        protocol: TCP
      - name: health
        containerPort: {{ $ctx.Values.noded.healthPort }}
        protocol: TCP
      # Node-local activator (ADR embervm/018 Fork A): the HTTP listener a node
      # Envoy routes a scaled-to-zero serving workload's first request to, so the
      # cold boot happens on the brick and survives a CP Recreate.
      - name: activator
        containerPort: {{ $ctx.Values.noded.activatorPort }}
        protocol: TCP
    env:
      - name: EMBERVM_NODED_LISTEN_ADDR
        value: ":{{ $ctx.Values.noded.grpcPort }}"
      - name: EMBERVM_NODED_HEALTH_ADDR
        value: ":{{ $ctx.Values.noded.healthPort }}"
      # The daemon self-identifies from the Downward API so snapshot node-
      # pinning is correct wherever it lands.
      - name: EMBERVM_NODED_NODE
        valueFrom:
          fieldRef:
            fieldPath: spec.nodeName
      # noded's own routable pod IP. Serving VMs are published as pod_ip:vmPort
      # and reached through a per-VM prerouting DNAT rule noded installs (to the
      # node-local tap), so a pod-network Envoy on any node can dial them
      # (D-R3.11.4). Unset falls back to reporting node-internal tap IPs.
      - name: EMBERVM_NODED_POD_IP
        valueFrom:
          fieldRef:
            fieldPath: status.podIP
      # Node-local activator listener (ADR embervm/018 Fork A). The daemon serves
      # cold-boot wakes for scaled-to-zero serving workloads here, and advertises
      # its own pod IP + this port as NodeStatus.activator_endpoint (the address a
      # pod-network Envoy dials, exactly as it dials serving VMs at pod_ip:vmPort;
      # advertising the node IP would black-hole since the serving DNAT lives in
      # noded's own netns). The pod IP is stable across a control-plane Recreate,
      # which is the failure this survives.
      - name: EMBERVM_NODED_ACTIVATOR_ADDR
        value: ":{{ $ctx.Values.noded.activatorPort }}"
      # The pod's own UID (Downward API metadata.uid), the daemon's INSTANCE
      # identity. Reported as NodeStatus.pod_uid and advertised in the
      # dial-home registration, so the control plane keys its registry and
      # capacity ledger by (node, pod_uid): two noded instances on one node
      # during a surge roll never alias (R0 PR-2, ADR embervm/005).
      - name: EMBERVM_POD_UID
        valueFrom:
          fieldRef:
            fieldPath: metadata.uid
      {{- if .sizeClass }}
      # Brick size-class (brick-capacity, ADR embervm/013 as amended): this
      # instance was deployed as a fixed-size brick of this T-shirt class. The
      # daemon echoes it as NodeStatus.size_class so the control plane's
      # BrickLedger buckets the instance by class and places whole VMs onto a
      # brick of the matching class. Unset on the legacy DaemonSet, whose pods
      # carry no class (the wildcard the ledger treats as matching every
      # request), which is what keeps the DS pod identical to its pre-brick form.
      - name: EMBERVM_NODED_SIZE_CLASS
        value: {{ .sizeClass | quote }}
      # Class-scoped because the flag is driver-wide per brick and sessions
      # place only on the listed class. An empty list renders nothing.
      {{- if and .sizeClass (has .sizeClass ($ctx.Values.noded.warmRestoreWithVolumeClasses | default (list))) }}
      - name: EMBERVM_NODED_WARM_RESTORE_WITH_VOLUME
        value: "true"
      {{- end }}
      {{- end }}
      # Dial-home registration target: the control plane's HTTP base URL the
      # daemon POSTs {node, pod_uid, address, boot_id} to on start and on a
      # jittered interval. Rendered from the control-plane Service name +
      # http port so it survives a rename. The daemon presents its projected
      # ServiceAccount token (auto-mounted at the default path) as the bearer.
      - name: EMBERVM_NODED_CONTROL_PLANE_URL
        value: {{ printf "http://%s.%s.svc:%v" (include "embervm.fullname" $ctx) $ctx.Release.Namespace $ctx.Values.service.port | quote }}
      - name: EMBERVM_NODED_MAX_LIVE_VMS
        value: {{ $ctx.Values.noded.maxLiveVMs | quote }}
      - name: EMBERVM_NODED_DIFF_BANKING
        value: {{ $ctx.Values.noded.diffBanking | quote }}
      - name: EMBERVM_NODED_DIFF_BANKING_WORKLOADS
        value: {{ $ctx.Values.noded.diffBankingWorkloads | quote }}
      # Brick warmth liveness claim (#4962). The daemon parses durations with Go
      # duration syntax. Only the exact string "1" enables unclaimed reaping;
      # "0" keeps the rollout transition guard off by default.
      - name: EMBERVM_NODED_WARMTH_HEARTBEAT_INTERVAL
        value: "{{ $ctx.Values.noded.warmthHeartbeat.intervalSeconds }}s"
      - name: EMBERVM_NODED_WARMTH_STALE_AFTER
        value: "{{ $ctx.Values.noded.warmthHeartbeat.staleAfterSeconds }}s"
      - name: EMBERVM_NODED_REAP_UNCLAIMED_WARMTH
        value: {{ ternary "1" "0" $ctx.Values.noded.warmthHeartbeat.reapUnclaimed | quote }}
      - name: EMBERVM_NODED_DAEMON_RESERVE_MIB
        value: {{ $ctx.Values.bricks.daemonReserveMib | default 512 | quote }}
      # Serving tap pre-provisioning (ADR embervm/014 decision 4). Zero (default)
      # disables it; the daemon clamps a positive value to its own cgroup-derived
      # slot ceiling regardless of what is configured here (server.go SlotCeiling).
      - name: EMBERVM_NODED_TAP_PREALLOC
        value: {{ $ctx.Values.noded.tapPrealloc | quote }}
      # R5 composite-group supernet: noded carves a /24 per group out of this
      # supernet and VALIDATES each control-plane-assigned group cidr is a /24
      # wholly within it. Distinct from the serving 172.31/12 tap space so the
      # two classes never share address space. Mirrors how the serving subnet
      # is a daemon config knob (defaulted in config.go, overridable here).
      - name: EMBERVM_NODED_COMPOSITE_SUPERNET
        value: {{ $ctx.Values.noded.compositeSupernet | quote }}
      # The DNAT port base for the deterministic per-VM/entry port space
      # (vmPort = base + hostOffset). Rendered from the SAME noded.servingPortBase
      # value the control plane's EMBERVM_COMPOSITE_PORT_BASE renders from, so the
      # CP's entry-endpoint port re-derivation stays in lockstep with the daemon's
      # PortForIP. noded defaults this to 30000 when unset (config.go).
      - name: EMBERVM_NODED_SERVING_PORT_BASE
        value: {{ $ctx.Values.noded.servingPortBase | quote }}
      # Per-invoke FC bundle snapshots and the fixed vsock dir the snapshot
      # embeds both derive from nvmeRoot so the scratch disk is one knob.
      - name: EMBERVM_NODED_SNAPSHOT_ROOT
        value: {{ printf "%s/embervm-noded/snapshots" $ctx.Values.noded.firecracker.nvmeRoot | quote }}
      - name: EMBERVM_NODED_CANONICAL_VSOCK_DIR
        value: {{ printf "%s/embervm-noded-vsock" $ctx.Values.noded.firecracker.nvmeRoot | quote }}
      - name: EMBERVM_NODED_GUEST_OOM_SCORE_ADJ
        value: {{ $ctx.Values.noded.firecracker.guestOomScoreAdj | quote }}
      - name: EMBERVM_NODED_BOOT_READY_TIMEOUT
        value: {{ $ctx.Values.noded.firecracker.bootReadyTimeout | quote }}
      {{- with $ctx.Values.noded.firecracker.kernelBootArgs }}
      - name: EMBERVM_NODED_KERNEL_BOOT_ARGS
        value: {{ . | quote }}
      {{- end }}
      - name: EMBERVM_NODED_DRAIN_TIMEOUT
        value: "{{ $ctx.Values.noded.drain.timeoutSeconds }}s"
      {{- if $ctx.Values.egress.enabled }}
      # Guest egress lane (ADR 023). Serve the vsock egress port per guest and
      # tunnel to the sidecar above. The workload list is load-bearing because
      # the sidecar has no client authentication and holds the real credential.
      # Absent when disabled, so the daemon's default stays off.
      - name: EMBERVM_NODED_EGRESS_ENABLED
        value: "true"
      - name: EMBERVM_NODED_EGRESS_SIDECAR_ADDR
        value: "127.0.0.1:8888"
      # The allowlist is DERIVED from the workloads that actually consume the
      # lane, never hand-copied. An explicit egress.workloads wins; otherwise the
      # enabled runtime workloads' own names are used. A second copy of a name,
      # policed by nothing, is the coupling this chart deleted when placeholder
      # substitution went away.
      #
      # This covers the CR and the allowlist, which move together. It does NOT
      # cover sessions banked before a rename: session.workload is durable and is
      # replayed as the relight trace, so those resume under the old name, miss
      # the new allowlist, and come back with no egress lane.
      {{- $egressWorkloads := $ctx.Values.egress.workloads }}
      {{- if not $egressWorkloads }}
      {{- $derived := list }}
      {{- if $ctx.Values.claudeRuntimeWorkload.enabled }}
      {{- $derived = append $derived $ctx.Values.claudeRuntimeWorkload.name }}
      {{- end }}
      {{- /*
        pi-runtime is the second consumer of the lane and needs it for the SAME
        reason the claude runtime does: its guest points a client at an egress
        URL (the in-cluster vLLM endpoint on egress.internal.allowlist). A
        runtime workload enabled but missing from this list gets no forwarder,
        which does not deny loudly, it dial-times-out inside the guest.
        Derived from the enabled workloads rather than hand-listed, so a new
        runtime workload cannot be added without its lane.
      */}}
      {{- if $ctx.Values.piRuntimeWorkload.enabled }}
      {{- $derived = append $derived $ctx.Values.piRuntimeWorkload.name }}
      {{- end }}
      {{- /*
        shotter (ADR embervm/035) is the third consumer and the first TASK-class
        one. It needs the lane because the whole point of the workload is to
        fetch a page: its guest points Chromium at an in-guest proxy that
        tunnels over vsock to this sidecar. Same silent failure mode as the two
        above, a workload enabled but missing here gets no forwarder and
        dial-times-out inside the guest rather than denying loudly.

        Note the comment above about this being "derived rather than
        hand-listed" describes the intent, not the mechanism: this is a
        hand-written if-chain over named workloads, so every new consumer needs
        its own arm here. Guarding on .enabled is why the chart default must
        define shotterWorkload.enabled, otherwise a values file lacking the key
        render-errors the whole pod template.

        The sidecar's policy is shared (external: allow, plus credential
        injection toward the hosts in egress.secrets), so granting this lane
        does NOT mean the snapshotter may dial anywhere. What confines it is the
        allowlist baked into its own image at /etc/shotter-egress.json, which
        refuses any destination outside the two frontend services before a vsock
        connection is opened. That is deliberate: see ADR embervm/035 section 4.
      */}}
      {{- if $ctx.Values.shotterWorkload.enabled }}
      {{- $derived = append $derived $ctx.Values.shotterWorkload.name }}
      {{- end }}
      {{- $egressWorkloads = $derived }}
      {{- end }}
      {{- if not $egressWorkloads }}
      {{- $egressWorkloads = list "__no_workload__" }}
      {{- end }}
      # ALWAYS emitted while egress is on, never omitted. The daemon reads an
      # absent list as "every workload", so omitting this on the one path that
      # derives nothing (egress.enabled with claudeRuntimeWorkload disabled)
      # would hand every task, session, stateful and group guest on the node a
      # forwarder to a sidecar that still holds the real credential, because
      # egress.secrets is a separate key that does not move with it. Disabling
      # the claude runtime during an incident would silently restore the very
      # capability this scoping removed. The sentinel is not a legal Kubernetes
      # object name (underscores), so it matches nothing and denies everything.
      - name: EMBERVM_NODED_EGRESS_WORKLOADS
        value: {{ join "," $egressWorkloads | quote }}
      {{- end }}
      # R1 zip lane: bounds a single archive HTTP GET and caps the fetched
      # bytes. The archive_url is minted by the control plane (fully-qualified
      # SeaweedFS S3 read URL); noded fetches it on the pod network. See
      # noded.zipLane in values.yaml for the read-path decision.
      - name: EMBERVM_NODED_ARCHIVE_FETCH_TIMEOUT
        value: {{ $ctx.Values.noded.zipLane.fetchTimeout | quote }}
      - name: EMBERVM_NODED_ARCHIVE_MAX_BYTES
        value: {{ $ctx.Values.noded.zipLane.maxBytes | quote }}
      # R6 off-node durability: the S3-API object store the continuity verbs
      # (ExportArtifact/RestoreArtifact/EvictArtifact) move banked artifacts to
      # and from. Optional static credentials enable SigV4. An EMPTY endpoint disables the store: exports are skipped
      # and restore-on-miss is impossible, so state stays local-only.
      - name: EMBERVM_NODED_STORE_ENDPOINT
        value: {{ $ctx.Values.noded.store.endpoint | quote }}
      - name: EMBERVM_NODED_STORE_BUCKET
        value: {{ $ctx.Values.noded.store.bucket | quote }}
      - name: EMBERVM_NODED_STORE_COMPRESS
        value: {{ $ctx.Values.noded.store.compress | quote }}
      {{- if $ctx.Values.noded.store.credentials.enabled }}
      - name: EMBERVM_NODED_STORE_ACCESS_KEY_ID
        valueFrom:
          secretKeyRef:
            name: {{ include "embervm.store.credentialsSecretName" $ctx }}
            key: {{ $ctx.Values.noded.store.credentials.accessKeyIdKey }}
      - name: EMBERVM_NODED_STORE_SECRET_ACCESS_KEY
        valueFrom:
          secretKeyRef:
            name: {{ include "embervm.store.credentialsSecretName" $ctx }}
            key: {{ $ctx.Values.noded.store.credentials.secretAccessKeyKey }}
      {{- end }}
      # R7 (ADR embervm/011, standing decision 4): the control plane becomes
      # the sole issuer of volume generations. false accepts a legacy
      # blessed_generation == 0 self-bump (the default, so this PR can land
      # both sides in one chart version without a wedge); true rejects an
      # unblessed writable attach FAILED_PRECONDITION. The CP and noded ship
      # from this ONE chart, so flipping this true here lands in the same
      # version the control plane starts blessing (never a mixed state).
      - name: EMBERVM_NODED_REQUIRE_BLESSING
        value: {{ $ctx.Values.noded.requireBlessing | quote }}
      # Artifact-decoupling Phase 2: the node-side image identity table that
      # USED to be rendered here as EMBERVM_NODED_IMAGES is retired. The daemon
      # boots with an EMPTY workload registry and the control plane PUSHES it
      # over SyncRegistry on connect (rootfs ref, harness init, sizing keyed by
      # workload), so image identity lives on the CP side now (see the control
      # plane's EMBERVM_NODE_IMAGE_IDENTITY). Readiness gates on that replay
      # (the /readyz probe below), so Service traffic never reaches a pod with
      # an empty registry.
      {{- if $ctx.Values.noded.bearerTokenSecret.enabled }}
      # Static bearer token gating the gRPC surface. The control plane reads
      # this same Secret under the same enabled flag, so enforcement and client
      # attachment always flip in one chart release.
      - name: EMBERVM_NODED_BEARER_TOKEN
        valueFrom:
          secretKeyRef:
            name: {{ include "embervm.noded.bearerTokenSecretName" $ctx }}
            key: {{ $ctx.Values.noded.bearerTokenSecret.key }}
      {{- end }}
    # Readiness gates on the control-plane registry replay (artifact-
    # decoupling Phase 2): /readyz is 200 only AFTER the first live
    # SyncRegistry, so the noded Service never routes to a pod with an empty
    # (or merely stale-cache) registry. Liveness stays on /healthz (always
    # 200 once the process is up) so a not-yet-synced pod is not restarted.
    readinessProbe:
      httpGet:
        path: /readyz
        port: health
      initialDelaySeconds: 2
      periodSeconds: 10
    livenessProbe:
      httpGet:
        path: /healthz
        port: health
      initialDelaySeconds: 5
      periodSeconds: 20
    resources:
      {{- toYaml .resources | nindent 6 }}
    volumeMounts:
      - name: dev-kvm
        mountPath: /dev/kvm
      - name: nvme
        mountPath: {{ $ctx.Values.noded.firecracker.nvmeRoot }}
{{- if $ctx.Values.egress.enabled }}
  # Egress-proxy sidecar (ADR 023). noded tunnels each guest's vsock egress to
  # this process over localhost; it is the only thing in the pod that reaches the
  # network on a guest's behalf. Keeping it a separate container is the point: the
  # daemon parses guest control frames but never egress bytes and holds no
  # credential, so a daemon compromise does not hand over the secrets.
  # nosemgrep: require-readiness-probe
  - name: egress-proxy
    image: "{{ $ctx.Values.egress.image.repository }}@{{ $ctx.Values.egress.image.digest }}"
    imagePullPolicy: {{ $ctx.Values.noded.image.pullPolicy | default "IfNotPresent" }}
    securityContext:
      runAsNonRoot: true
      runAsUser: 65532
      runAsGroup: 65532
      allowPrivilegeEscalation: false
      readOnlyRootFilesystem: true
      capabilities:
        drop:
          - ALL
    env:
      # Must match EMBERVM_NODED_EGRESS_SIDECAR_ADDR on the noded container. The
      # loopback bind is load-bearing: this sidecar has no client authentication,
      # so it is the only barrier to an arbitrary cluster workload using the
      # credentialed response path.
      - name: EGRESS_LISTEN
        value: "127.0.0.1:8888"
      # Split-horizon guardrail. Secret egressTo hosts are external and are
      # reached by external:allow, so they never belong in the internal allowlist.
      - name: EGRESS_EXTERNAL
        value: {{ $ctx.Values.egress.external | default "allow" | quote }}
      - name: EGRESS_INTERNAL_DEFAULT
        value: {{ $ctx.Values.egress.internal.default | default "deny" | quote }}
      {{- with $ctx.Values.egress.internal.allowlist }}
      - name: EGRESS_INTERNAL_ALLOWLIST
        value: {{ join "," . | quote }}
      {{- end }}
      {{- with $ctx.Values.egress.internal.cidrs }}
      - name: EGRESS_INTERNAL_CIDRS
        value: {{ join "," . | quote }}
      {{- end }}
      {{- if $ctx.Values.egress.secrets }}
      # Phase 6b. EGRESS_SECRETS is the NON-secret catalog (which header to set on
      # which hosts, and which env carries the value); each real value arrives
      # separately from its own Secret below, so the catalog itself is safe to
      # render into the pod spec.
      {{- $catalog := list }}
      {{- range $s := $ctx.Values.egress.secrets }}
      {{- $hasSecretRef := and (hasKey $s "secretRef") (not (empty $s.secretRef)) }}
      {{- $hasBrokerGrant := and (hasKey $s "brokerGrant") (not (empty $s.brokerGrant)) }}
      {{- if or (and $hasSecretRef $hasBrokerGrant) (not (or $hasSecretRef $hasBrokerGrant)) (and $hasSecretRef (empty $s.env)) }}
      {{- fail (printf "egress.secrets entry for %v must set exactly one of secretRef or brokerGrant, and secretRef entries need env" $s.egressTo) }}
      {{- end }}
      {{- if $hasBrokerGrant }}
      {{- $catalog = append $catalog (dict "header" $s.header "valuePrefix" ($s.valuePrefix | default "") "basicUser" ($s.basicUser | default "") "brokerGrant" $s.brokerGrant "egressTo" $s.egressTo "claimHeader" ($s.claimHeader | default "") "claimPath" ($s.claimPath | default "") "injectAlwaysPaths" ($s.injectAlwaysPaths | default (list))) }}
      {{- else }}
      {{- $catalog = append $catalog (dict "header" $s.header "valuePrefix" ($s.valuePrefix | default "") "basicUser" ($s.basicUser | default "") "env" $s.env "egressTo" $s.egressTo "claimHeader" ($s.claimHeader | default "") "claimPath" ($s.claimPath | default "") "injectAlwaysPaths" ($s.injectAlwaysPaths | default (list))) }}
      {{- end }}
      {{- end }}
      - name: EGRESS_SECRETS
        value: {{ $catalog | toJson | quote }}
      {{- $hasBroker := false }}
      {{- range $s := $ctx.Values.egress.secrets }}
      {{- if and (hasKey $s "brokerGrant") (not (empty $s.brokerGrant)) }}{{- $hasBroker = true }}{{- end }}
      {{- end }}
      {{- if $hasBroker }}
      - name: EGRESS_TOKEN_BROKER_URL
        value: {{ printf "%s.%s.svc.cluster.local:8080" (include "embervm.tokenBroker.fullname" $ctx) $ctx.Release.Namespace | quote }}
      {{- end }}
      {{- if $ctx.Values.egress.ca.enabled }}
      # Optional TLS-MITM lane, for a guest that speaks https:// to the sidecar and
      # already trusts this CA. The claude runtime does NOT: it speaks cleartext
      # over its host-local vsock, so the swap needs no CA at all. Turning this on
      # means owning the CA's path into the guest trust store, which is why it is
      # off rather than implied by having secrets.
      - name: EGRESS_CA_CERT_FILE
        value: /etc/egress-ca/tls.crt
      - name: EGRESS_CA_KEY_FILE
        value: /etc/egress-ca/tls.key
      {{- end }}
      {{- range $s := $ctx.Values.egress.secrets }}
      {{- if and (hasKey $s "secretRef") (not (empty $s.secretRef)) }}
      # secretRef ONLY. There is deliberately no literal-value branch: a chart that
      # accepts an inline credential is a chart someone eventually commits one to.
      # optional: a catalog entry whose secret FIELD does not exist yet is the
      # supported deferred state (the sidecar keeps the entry dead and DENIES its
      # hosts); without optional the kubelet fails container creation on the
      # missing key and wedges the brick roll (observed live, 0.1.350).
      - name: {{ $s.env }}
        valueFrom:
          secretKeyRef:
            name: {{ $s.secretRef.name }}
            key: {{ $s.secretRef.key }}
            optional: true
      {{- end }}
      {{- end }}
      {{- end }}
    {{- if $ctx.Values.egress.ca.enabled }}
    volumeMounts:
      - name: egress-ca
        mountPath: /etc/egress-ca
        readOnly: true
    {{- end }}
    resources:
      {{- toYaml $ctx.Values.egress.resources | nindent 6 }}
{{- end }}
volumes:
{{- if and $ctx.Values.egress.enabled $ctx.Values.egress.ca.enabled }}
  - name: egress-ca
    secret:
      secretName: {{ $ctx.Values.egress.ca.secretName | default (printf "%s-egress-ca" (include "embervm.fullname" $ctx)) }}
      # optional, for the same reason the secretRef entries above are optional:
      # without it the kubelet fails container creation on a missing Secret and
      # WEDGES THE BRICK ROLL (observed live at 0.1.350, and again at 0.1.451
      # when this volume was added non-optional). cert-manager issues this
      # Secret from a Certificate in the SAME sync that adds the mount, and
      # ArgoCD blocks that sync waiting for the brick Deployment to go healthy,
      # so the mount can be waiting on a Secret whose creation is waiting on the
      # mount. Optional breaks the deadlock.
      #
      # Degrading is safe: the sidecar treats an unreadable CA as "no MITM lane"
      # (newCAMinter fails, minter stays nil) and keeps injecting on the
      # cleartext lane, so a guest loses https:// credential injection rather
      # than all egress.
      optional: true
{{- end }}
  - name: dev-kvm
    hostPath:
      path: /dev/kvm
      type: CharDevice
  - name: nvme
    hostPath:
      path: {{ $ctx.Values.noded.firecracker.nvmeRoot }}
      # Directory (the default) is a GUARD, not a formality: if the NVMe is not
      # mounted, the pod fails to start rather than silently creating the path on
      # the node's ROOT filesystem and filling it with multi-GB base images.
      # Production must keep it.
      #
      # DirectoryOrCreate is for a path UNDER an already-mounted parent, where
      # the mount is guaranteed by the parent's existence and only the leaf needs
      # creating. embervm-dev uses that shape: scratch/dev under node-4's NVMe,
      # so it inherits the NVMe's capacity rather than living uncapped on root
      # (ADR 012's uniform cap, and #4832).
      type: {{ $ctx.Values.noded.firecracker.nvmeRootHostPathType | default "Directory" }}
  {{- if $ctx.Values.workloads }}
  - name: rootfs-builder-script
    configMap:
      name: {{ include "embervm.noded.fullname" $ctx }}-rootfs-builder
      defaultMode: 0755
  - name: rootfs-builder-work
    emptyDir: {}
  {{- if $ctx.Values.imagePullSecret.enabled }}
  - name: ghcr-creds
    secret:
      secretName: {{ $ctx.Values.imagePullSecret.name }}
      items:
        - key: .dockerconfigjson
          path: config.json
  {{- end }}
  {{- end }}
{{- end -}}
