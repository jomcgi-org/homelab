# gazelle:ignore
load("@aspect_bazel_lib//lib:tar.bzl", "tar")
load("@aspect_bazel_lib//lib:transitions.bzl", "platform_transition_filegroup")
load("//bazel/tools/oci:apko_image.bzl", "apko_image")

# EmberVM scratch-postgres runtime base image (R4 stateful class, ADR
# embervm/001, D-R4.PR-11.1). A Wolfi apko base (postgresql-16 + e2fsprogs +
# busybox) whose PID 1 is ember-postgres-init (guest-init/cmd). On a base build
# it answers the vsock /shim/ready contract so noded snapshots the warm base; on
# a stateful cold boot it mounts the writable volume, decodes POSTGRES_PASSWORD
# from the mmds_env boot-args, runs initdb-if-empty (scram auth, the `scratch`
# database), and launches postgres on TCP 5432. amd64 only, mirroring the python
# runtime layout.

# ember-postgres-init, the PID-1 init: cross-compiled per arch and layered at
# /usr/local/bin/ember-postgres-init (the noded harnessInit boots it via
# init=<path>). Mirrors the python runtime guest-init packaging.
platform_transition_filegroup(
    name = "guest_init_amd64_bin",
    srcs = ["//projects/embervm/runtimes/postgres/guest-init/cmd"],
    target_platform = "@rules_go//go/toolchain:linux_amd64",
)

tar(
    name = "guest_init_tar_amd64",
    srcs = [":guest_init_amd64_bin"],
    mtree = [
        "./usr/local/bin/ember-postgres-init type=file content=$(execpath :guest_init_amd64_bin) mode=0755 uid=0 gid=0",
    ],
)

apko_image(
    name = "image",
    # amd64 only: see the `arm64` arg in apko_image.bzl.
    arm64 = False,
    config = "apko.yaml",
    contents = "@embervm_runtime_postgres_lock//:contents",
    repository = "ghcr.io/jomcgi/homelab/projects/embervm/runtimes/postgres",
    tars = [
        ":guest_init_tar_amd64",
    ],
)
