load("//bazel/helm:defs.bzl", "helm_chart")

# Firecracker snapshot/restore controller chart (ADR 022). Image tags are pinned
# into values at build time from each image's .info provider: the controller
# itself, the harness image the base rootfs is built from, and the rootfs-builder
# tools image its initContainer runs.
helm_chart(
    name = "chart",
    images = {
        "image": "//projects/agent_platform/fc-agentd:image.info",
        "harnessImage": "//projects/agent_platform/harness:image.info",
        "rootfsBuilder.image": "//projects/agent_platform/fc-agentd/rootfs-builder:image.info",
        "egress.image": "//projects/agent_platform/egress-proxy:image.info",
    },
    publish = True,
    visibility = ["//overlays:__subpackages__"],
)
