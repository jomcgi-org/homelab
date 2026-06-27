load("//bazel/helm:defs.bzl", "helm_chart")

# Firecracker snapshot/restore controller chart (ADR 022). The image tag is
# pinned into values.image at build time from the go_image .info provider.
helm_chart(
    name = "chart",
    images = {
        "image": "//projects/agent_platform/fc-agentd:image.info",
    },
    publish = True,
    visibility = ["//overlays:__subpackages__"],
)
