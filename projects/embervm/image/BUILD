load("//bazel/tools/oci:apko_image.bzl", "apko_image")

# EmberVM control-plane image: the Wolfi + erlang-27 base (apko.yaml) with the
# control-plane OTP release layered at /opt/embervm. AMD64-ONLY: the release
# carries exqlite's arch-specific sqlite3_nif.so, so the tar is not portable
# across arches; the executor and every cluster node are amd64 (see apko.yaml for
# the full rationale). Entrypoint (from apko.yaml) is /opt/embervm/bin/embervm start.
apko_image(
    name = "image",
    config = "apko.yaml",
    contents = "@embervm_lock//:contents",
    repository = "ghcr.io/jomcgi/homelab/projects/embervm/image",
    tars = ["//projects/embervm/control:release_tar"],
)
