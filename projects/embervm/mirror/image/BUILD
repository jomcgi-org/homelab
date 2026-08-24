# gazelle:ignore
load("@aspect_bazel_lib//lib:tar.bzl", "tar")
load("@aspect_bazel_lib//lib:transitions.bzl", "platform_transition_filegroup")
load("//bazel/tools/oci:apko_image.bzl", "apko_image")

# Node-local git mirror sidecar image (#4473): the Wolfi base (git + busybox)
# with the embervm-git-mirror Go server layered at /usr/local/bin. Runs as one
# non-root container in every noded pod; see apko.yaml for the uid reasoning.
#
# amd64 only: see the `arm64` arg in apko_image.bzl and the comment in
# apko.yaml.

platform_transition_filegroup(
    name = "git_mirror_amd64_bin",
    srcs = ["//projects/embervm/mirror/cmd:git-mirror"],
    target_platform = "@rules_go//go/toolchain:linux_amd64",
)

tar(
    name = "git_mirror_tar_amd64",
    srcs = [":git_mirror_amd64_bin"],
    mtree = [
        "./usr/local/bin/embervm-git-mirror type=file content=$(execpath :git_mirror_amd64_bin) mode=0755 uid=0 gid=0",
    ],
)

apko_image(
    name = "image",
    # amd64 only: see the `arm64` arg in apko_image.bzl.
    arm64 = False,
    config = "apko.yaml",
    contents = "@embervm_mirror_lock//:contents",
    repository = "ghcr.io/jomcgi/homelab/projects/embervm/mirror/image",
    tars = [
        ":git_mirror_tar_amd64",
    ],
)
