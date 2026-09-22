load("//bazel/tools/oci:apko_image.bzl", "apko_image")

# embervm scratch-prep DaemonSet container image: a minimal Wolfi image carrying
# busybox (shell), util-linux, e2fsprogs, xfsprogs and psmisc. The chart mounts
# the versioned prep script from a ConfigMap. Filesystem inspection, formatting
# and consumer checks use this image's tools; host mount identity and mount
# operations run through nsenter. amd64 only, matching the deployed brick fleet.
apko_image(
    name = "image",
    # amd64 only: see the `arm64` arg in apko_image.bzl.
    arm64 = False,
    config = "apko.yaml",
    contents = "@embervm_scratch_prep_lock//:contents",
    repository = "ghcr.io/jomcgi/homelab/projects/embervm/scratch-prep",
)
