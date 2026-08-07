load("//bazel/helm:defs.bzl", "helm_chart")

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
        "sandbox.guestImage": "//projects/firecracker/sandbox/guest:image.info",
        "rootfsBuilder.image": "//projects/firecracker/substrate/rootfs-builder:image.info",
        # Task 14b: the frozen fc-invoke semgrep guest. Distinct top-level key.
        "semgrep.guestImage": "//projects/firecracker/semgrep/guest:image.info",
        # R1 zip lane (ADR embervm/002): the runtime-python base image the zip
        # lane layers an adopter's archive onto. Distinct top-level key (D14a.6).
        "runtimePython.guestImage": "//projects/embervm/runtimes/python:image.info",
        # The egress-proxy sidecar (ADR 023). Shared with fc-invoke rather than
        # forked: same binary, same posture, one place to fix. Pinned from its
        # .info so the sidecar the daemon tunnels to is the digest CI built.
        "egress.image": "//projects/firecracker/substrate/egress-proxy:image.info",
        # Issue #4055: the runtime-claude base image, a session-class guest that
        # runs the Claude Code CLI headlessly under a vsock shim so monolith can
        # expose agent sessions as MCP tools. Distinct FLAT top-level key (D14a.6),
        # digest-pinned from its .info so the rootfs-builder GUEST_IMAGE, the
        # EMBERVM_NODED_IMAGES entry, and the Workload's source.image.ref all
        # resolve to the same digest.
        "runtimeClaude.guestImage": "//projects/embervm/runtimes/claude:image.info",
        # R4 stateful consumer (D-R4.PR-11.1): the scratch-postgres base image.
        # Distinct FLAT top-level key (D14a.6), digest-pinned from its .info.
        "scratchPostgres.guestImage": "//projects/embervm/runtimes/postgres:image.info",
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
