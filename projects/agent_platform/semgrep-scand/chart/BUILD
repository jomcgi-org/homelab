load("//bazel/helm:defs.bzl", "helm_chart")

# semgrep-scand daemon chart. Image tags are pinned into values at build time from
# each image's .info provider: the daemon itself, the semgrep-guest image the base
# rootfs is built from, and the rootfs-builder tools image its initContainer runs
# (reused from fc-agentd, a generic crane + mkfs.ext4 image).
helm_chart(
    name = "chart",
    images = {
        "image": "//projects/agent_platform/semgrep-scand:image.info",
        "guestImage": "//projects/firecracker/semgrep/guest:image.info",
        "rootfsBuilder.image": "//projects/agent_platform/fc-agentd/rootfs-builder:image.info",
    },
    publish = True,
    visibility = ["//overlays:__subpackages__"],
)
