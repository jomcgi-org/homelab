load("//bazel/helm:defs.bzl", "helm_chart")

# EmberVM control-plane chart. The control-plane image tag is pinned into
# values.image at build time from the image's .info provider (CI rebuilds the
# image with a stamped tag; the chart version bump is what ArgoCD pulls).
helm_chart(
    name = "chart",
    images = {
        "image": "//projects/embervm/image:image.info",
        "noded.image": "//projects/embervm/noded/image:image.info",
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
    },
    publish = True,
    visibility = ["//overlays:__subpackages__"],
)
