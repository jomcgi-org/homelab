load("//bazel/tools/oci:apko_image.bzl", "apko_image")

# embervm scratch-prep DaemonSet container image: a minimal Wolfi image carrying
# only busybox (shell) + util-linux (nsenter). No Go binary, no repo code: the
# DaemonSet's command (rendered in the chart template) is the whole program,
# invoking host binaries in the host mount namespace via nsenter. DUAL-ARCH,
# matching every other apko image in the repo, though node-4 (the only node
# with scratchPrep.nodeMap entries today) is amd64.
apko_image(
    name = "image",
    config = "apko.yaml",
    contents = "@embervm_scratch_prep_lock//:contents",
    repository = "ghcr.io/jomcgi/homelab/projects/embervm/scratch-prep",
)
