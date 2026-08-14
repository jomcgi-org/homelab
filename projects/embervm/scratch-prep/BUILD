load("//bazel/tools/oci:apko_image.bzl", "apko_image")

# embervm scratch-prep DaemonSet container image: a minimal Wolfi image carrying
# busybox (shell) + util-linux (nsenter, fallocate, losetup, blkid) + e2fsprogs
# (mkfs.ext4). No Go binary, no repo code: the DaemonSet's command (rendered in
# the chart template) is the whole program. It provisions a uniform size-capped
# ext4 loop scratch using THIS image's own fs tooling (so it does not depend on
# host e2fsprogs/util-linux), and runs only the final `mount` in the host mount
# namespace via nsenter. amd64 only, matching every other apko image in the repo.
apko_image(
    name = "image",
    # amd64 only: see the `arm64` arg in apko_image.bzl.
    arm64 = False,
    config = "apko.yaml",
    contents = "@embervm_scratch_prep_lock//:contents",
    repository = "ghcr.io/jomcgi/homelab/projects/embervm/scratch-prep",
)
