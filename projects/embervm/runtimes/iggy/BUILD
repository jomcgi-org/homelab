# gazelle:ignore
load("@aspect_bazel_lib//lib:tar.bzl", "tar")
load("@aspect_bazel_lib//lib:transitions.bzl", "platform_transition_filegroup")
load("//bazel/tools/oci:apko_image.bzl", "apko_image")

# EmberVM iggy runtime base image (R4 stateful class, ADR embervm/039). A thin
# Wolfi apko base (busybox + e2fsprogs + blkid) carrying the vendored static
# iggy-server binary, whose PID 1 is ember-iggy-init (guest-init/cmd). On a base
# build it answers the vsock /shim/ready contract so noded snapshots the warm
# base; on a stateful cold boot it mounts the writable volume, decodes
# IGGY_ROOT_PASSWORD from the mmds_env boot-args, and launches iggy-server on TCP
# 8090 with its data root on the volume. amd64 only, mirroring the postgres
# runtime layout it is modelled on.

# ember-iggy-init, the PID-1 init: cross-compiled per arch and layered at
# /usr/local/bin/ember-iggy-init (the noded harnessInit boots it via init=<path>).
platform_transition_filegroup(
    name = "guest_init_amd64_bin",
    srcs = ["//projects/embervm/runtimes/iggy/guest-init/cmd"],
    target_platform = "@rules_go//go/toolchain:linux_amd64",
)

tar(
    name = "guest_init_tar_amd64",
    srcs = [":guest_init_amd64_bin"],
    mtree = [
        "./usr/local/bin/ember-iggy-init type=file content=$(execpath :guest_init_amd64_bin) mode=0755 uid=0 gid=0",
    ],
)

# The Apache Iggy server (and its CLI), lifted out of the digest-pinned
# apache/iggy image by //bazel/tools/oci:oci_binaries. Docker Hub is the server's
# only durable distribution channel, so this is an image extraction rather than
# the multiarch_http_archive vendoring the k3s and goose binaries use; see the
# rule's docstring and ADR embervm/039. Both files are static musl builds (the
# rule asserts it on every fetch), so they carry no library closure into this
# layer and the rootfs above stays thin.
tar(
    name = "iggy_server_tar_amd64",
    srcs = [
        "@iggy_server_bin//:usr/local/bin/iggy",
        "@iggy_server_bin//:usr/local/bin/iggy-server",
    ],
    mtree = [
        "./usr/local/bin/iggy-server type=file content=$(execpath @iggy_server_bin//:usr/local/bin/iggy-server) mode=0755 uid=0 gid=0",
        "./usr/local/bin/iggy type=file content=$(execpath @iggy_server_bin//:usr/local/bin/iggy) mode=0755 uid=0 gid=0",
    ],
)

apko_image(
    name = "image",
    # amd64 only: see the `arm64` arg in apko_image.bzl.
    arm64 = False,
    config = "apko.yaml",
    contents = "@embervm_runtime_iggy_lock//:contents",
    repository = "ghcr.io/jomcgi/homelab/projects/embervm/runtimes/iggy",
    tars = [
        ":iggy_server_tar_amd64",
        ":guest_init_tar_amd64",
    ],
)
