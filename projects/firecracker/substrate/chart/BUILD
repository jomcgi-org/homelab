load("//bazel/helm:defs.bzl", "helm_chart")

# fc-invoke daemon chart. Image tags are pinned into values at build time from
# each image's .info provider: the daemon itself, the per-workload guest images
# each workload's base rootfs is built from (semgrep, agent), the rootfs-builder
# tools image the initContainers run (a generic crane + mkfs.ext4 image), and the
# egress-proxy sidecar. Add a `<workload>.guestImage` pin here per new workload.
helm_chart(
    name = "chart",
    images = {
        "image": "//projects/firecracker/substrate/invoke:image.info",
        "semgrep.guestImage": "//projects/firecracker/semgrep/guest:image.info",
        "agent.guestImage": "//projects/firecracker/goosecracker/guest:image.info",
        "sandbox.guestImage": "//projects/firecracker/sandbox/guest:image.info",
        "rootfsBuilder.image": "//projects/firecracker/substrate/rootfs-builder:image.info",
        "egress.image": "//projects/firecracker/substrate/egress-proxy:image.info",
    },
    publish = True,
    visibility = ["//overlays:__subpackages__"],
)
