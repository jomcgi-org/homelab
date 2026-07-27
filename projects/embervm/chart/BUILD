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
    },
    publish = True,
    visibility = ["//overlays:__subpackages__"],
)
