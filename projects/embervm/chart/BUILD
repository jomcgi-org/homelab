load("//bazel/helm:defs.bzl", "helm_chart")
load("//bazel/semgrep/defs:defs.bzl", "semgrep_test")
load("//bazel/tools/pytest:defs.bzl", "py_test")

# EmberVM control-plane chart. The control-plane image tag is pinned into
# values.image at build time from the image's .info provider (CI rebuilds the
# image with a stamped tag; the chart version bump is what ArgoCD pulls).
helm_chart(
    name = "chart",
    images = {
        "image": "//projects/embervm/image:image.info",
        "noded.image": "//projects/embervm/noded/image:image.info",
        # R3 PR-3: the xDS sidecar image (go-control-plane ADS server), a second
        # container in the control-plane pod. Distinct top-level key "xds" (not
        # nested under a shared prefix), per the helm_images_values duplicate-key
        # caveat noted below.
        "xds.image": "//projects/embervm/xds/image:image.info",
        # Task 14: the frozen fc-invoke sandbox guest image (baked into a base
        # rootfs by the noded rootfs-builder) and the rootfs-builder tool image,
        # digest-pinned from their .info providers so the guest contract is
        # unchanged. Their .info targets are public (apko_image), so cross-project
        # references are allowed. These MUST be DISTINCT top-level keys (not
        # noded.*): helm_images_values emits one YAML block per dotted key, so two
        # keys sharing a top-level prefix (e.g. noded.image + noded.sandbox.*)
        # collapse to the last on parse (duplicate-key), dropping the others. Flat
        # top-level keys mirror fc-invoke's proven layout.
        "sandboxPython.guestImage": "//projects/firecracker/sandbox/python:image.info",
        "sandboxGo.guestImage": "//projects/firecracker/sandbox/go:image.info",
        "sandboxRust.guestImage": "//projects/firecracker/sandbox/rust:image.info",
        "sandboxElixir.guestImage": "//projects/firecracker/sandbox/elixir:image.info",
        "sandboxOcaml.guestImage": "//projects/firecracker/sandbox/ocaml:image.info",
        "sandboxJavascript.guestImage": "//projects/firecracker/sandbox/javascript:image.info",
        "rootfsBuilder.image": "//projects/firecracker/substrate/rootfs-builder:image.info",
        # Semgrep guest image. Distinct top-level key.
        "semgrep.guestImage": "//projects/firecracker/semgrep/guest:image.info",
        # R1 zip lane (ADR embervm/002): the runtime-python base image the zip
        # lane layers an adopter's archive onto. Distinct top-level key (D14a.6).
        "runtimePython.guestImage": "//projects/embervm/runtimes/python:image.info",
        # The egress-proxy sidecar (ADR 023). Pinned from its
        # .info so the sidecar the daemon tunnels to is the digest CI built.
        "egress.image": "//projects/firecracker/substrate/egress-proxy:image.info",
        # Issue #4055: the runtime-claude base image, a session-class guest that
        # runs the Claude Code CLI headlessly under a vsock shim so monolith can
        # expose agent sessions as MCP tools. Distinct FLAT top-level key (D14a.6),
        # digest-pinned from its .info so the rootfs-builder GUEST_IMAGE, the
        # EMBERVM_NODED_IMAGES entry, and the Workload's source.image.ref all
        # resolve to the same digest.
        "runtimeClaude.guestImage": "//projects/embervm/runtimes/claude:image.info",
        # The runtime-pi base image: the same session guest as runtime-claude
        # carrying only the pi CLI, so a qwen turn runs at 256 MiB instead of
        # 4096. Distinct FLAT top-level key (D14a.6), digest-pinned from its
        # .info so the rootfs-builder GUEST_IMAGE, the EMBERVM_NODED_IMAGES
        # entry, and the Workload's source.image.ref all resolve to the same
        # digest.
        "runtimePi.guestImage": "//projects/embervm/runtimes/pi:image.info",
        # First serving-class workload (#5180): the internal serving-path
        # prover, a minimal Go HTTP guest. Distinct FLAT top-level key,
        # digest-pinned from its .info.
        "pingWorkload.guestImage": "//projects/embervm/runtimes/ping:image.info",
        # R4 stateful consumer (D-R4.PR-11.1): the scratch-postgres base image.
        # Distinct FLAT top-level key (D14a.6), digest-pinned from its .info.
        "scratchPostgres.guestImage": "//projects/embervm/runtimes/postgres:image.info",
        # ADR embervm/035 (website snapshotter): the shotter base image, a warm
        # headless Chromium. Distinct FLAT top-level key (D14a.6), digest-pinned
        # from its .info so the rootfs-builder GUEST_IMAGE, the noded
        # EMBERVM_NODE_IMAGE_IDENTITY entry, and the Workload CR's
        # source.image.ref all resolve to the same digest. The digest also
        # covers the baked /etc/shotter-egress.json, which is the guest's
        # egress allowlist, so moving this pin is what changes the destination
        # policy.
        "shotter.guestImage": "//projects/embervm/runtimes/shotter:image.info",
        # ADR embervm/010 (Skyframe snapshot demo): the bazel-query base image (a
        # warm Bazel server over Abseil). Distinct FLAT top-level key (D14a.6),
        # digest-pinned from its .info so the noded EMBERVM_NODED_IMAGES ref, the
        # rootfs-builder GUEST_IMAGE, and the Workload CR's source.image.ref all
        # resolve to the same pinned digest.
        "bazelQuery.guestImage": "//projects/embervm/runtimes/bazel:image.info",
        "scratchPrep.image": "//projects/embervm/scratch-prep:image.info",
        "tokenBroker.image": "//projects/embervm/tokenbroker/image:image.info",
    },
    publish = True,
    visibility = ["//overlays:__subpackages__"],
)

# Exposed so the ENOSPC-reclaim guard in the claude runtime package can read the
# gate default and the builder script text. Mirrors
# projects/embervm/deploy:deploy_values, which the cleartext-lane guard reads the
# same way. The guard lives there, not here, because a chart package has no
# Python toolchain context.
filegroup(
    name = "rootfs_reclaim_sources",
    srcs = [
        "templates/noded-rootfs-builder-configmap.yaml",
        "values.yaml",
    ],
    visibility = ["//projects/embervm/runtimes/claude:__pkg__"],
)

# py_test is hand-registered: gazelle does not generate py_test rules, so they
# must be declared explicitly in BUILD.
py_test(
    name = "chart_env_identity_test",
    srcs = ["chart_env_identity_test.py"],
    data = [
        "Chart.yaml",
        "values.yaml",
        "//projects/embervm/deploy:application",
        "//projects/embervm/deploy:deploy_values",
        "//projects/embervm/dev/deploy:application",
        "//projects/embervm/dev/deploy:deploy_values",
        "@multitool//tools/helm",
    ] + glob([
        "templates/**/*",
        "crds/**/*",
    ]),
    env = {
        "HELM_BIN": "$(rootpath @multitool//tools/helm)",
        "PROD_VALUES": "$(rootpath //projects/embervm/deploy:deploy_values)",
        "DEV_VALUES": "$(rootpath //projects/embervm/dev/deploy:deploy_values)",
        # The Applications, so releaseName is read from where it is DECLARED.
        "PROD_APPLICATION": "$(rootpath //projects/embervm/deploy:application)",
        "DEV_APPLICATION": "$(rootpath //projects/embervm/dev/deploy:application)",
    },
    deps = ["@pip//pytest"],
)

py_test(
    name = "chart_sync_wave_test",
    srcs = ["chart_sync_wave_test.py"],
    data = [
        "Chart.yaml",
        "values.yaml",
        "@multitool//tools/helm",
    ] + glob([
        "templates/**/*",
        "crds/**/*",
    ]),
    env = {
        "HELM_BIN": "$(rootpath @multitool//tools/helm)",
    },
    deps = ["@pip//pytest"],
)

# Pins the noded CiliumNetworkPolicy's activator ports to the servingEnvoy
# capacity values: the 2026-08-22 cold-wake drop happened because the stateful
# and composite TCP ranges were missing from the policy with nothing failing
# CI. See noded_networkpolicy_test.py and the STPA open question it closes.
py_test(
    name = "noded_networkpolicy_test",
    srcs = ["noded_networkpolicy_test.py"],
    data = [
        "Chart.yaml",
        "values.yaml",
        "@multitool//tools/helm",
    ] + glob([
        "templates/**/*",
        "crds/**/*",
    ]),
    env = {
        "HELM_BIN": "$(rootpath @multitool//tools/helm)",
    },
    deps = ["@pip//pytest"],
)

py_test(
    name = "chart_hypervisor_epoch_test",
    srcs = ["chart_hypervisor_epoch_test.py"],
    data = [
        "values.yaml",
        "//:MODULE.bazel",
    ],
    env = {
        "MODULE_BAZEL": "$(rootpath //:MODULE.bazel)",
    },
    deps = ["@pip//pytest"],
)

semgrep_test(
    name = "chart_hypervisor_epoch_test_semgrep_test",
    srcs = ["chart_hypervisor_epoch_test.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "chart_sync_wave_test_semgrep_test",
    srcs = ["chart_sync_wave_test.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "chart_env_identity_test_semgrep_test",
    srcs = ["chart_env_identity_test.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

# Pins the two ADR-014-style control-plane gates (#5230) to their chart keys:
# EMBERVM_PLACEMENT_RETRY (dispatcher.placementRetry) and
# EMBERVM_ASYNC_LIFECYCLE_WRITES (asyncLifecycleWrites) must always render,
# defaulting "false" and flipping via --set alone. See
# chart_control_env_gate_test.py.
py_test(
    name = "chart_control_env_gate_test",
    srcs = ["chart_control_env_gate_test.py"],
    data = [
        "Chart.yaml",
        "values.yaml",
        "@multitool//tools/helm",
    ] + glob([
        "templates/**/*",
        "crds/**/*",
    ]),
    env = {
        "HELM_BIN": "$(rootpath @multitool//tools/helm)",
    },
    deps = ["@pip//pytest"],
)

semgrep_test(
    name = "chart_control_env_gate_test_semgrep_test",
    srcs = ["chart_control_env_gate_test.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "noded_networkpolicy_test_semgrep_test",
    srcs = ["noded_networkpolicy_test.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)
