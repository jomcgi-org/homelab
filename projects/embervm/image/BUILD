load("//bazel/tools/oci:apko_image.bzl", "apko_image")

# EmberVM control-plane image: the Wolfi + erlang-27 base (apko.yaml) with the
# control-plane OTP release layered at /opt/embervm. The release is
# architecture-independent .beam (include_erts:false), so it is a regular tar
# used for both arches; apko resolves erlang-27 per arch. Entrypoint (from
# apko.yaml) is /opt/embervm/bin/embervm start.
apko_image(
    name = "image",
    config = "apko.yaml",
    contents = "@embervm_lock//:contents",
    repository = "ghcr.io/jomcgi/homelab/projects/embervm/image",
    tars = ["//projects/embervm/control:release_tar"],
)
