load("//bazel/helm:defs.bzl", "helm_chart")

# fc-invoke daemon chart. Image tags are pinned into values at build time from
# each image's .info provider: the daemon itself, the semgrep-guest image the
# semgrep workload's base rootfs is built from, and the rootfs-builder tools
# image its initContainer runs (reused from fc-agentd, a generic crane +
# mkfs.ext4 image). Add a `<workload>.guestImage` pin here per new workload.
helm_chart(
    name = "chart",
    images = {
        "image": "//projects/firecracker/substrate/invoke:image.info",
        "semgrep.guestImage": "//projects/firecracker/semgrep/guest:image.info",
        "rootfsBuilder.image": "//projects/agent_platform/fc-agentd/rootfs-builder:image.info",
    },
    publish = True,
    visibility = ["//overlays:__subpackages__"],
)
