# gazelle:ignore
load("@aspect_bazel_lib//lib:tar.bzl", "tar")
load("@aspect_bazel_lib//lib:transitions.bzl", "platform_transition_filegroup")
load("@rules_pkg//pkg:tar.bzl", "pkg_tar")
load("//bazel/tools/oci:apko_image.bzl", "apko_image")

# Wolfi's lvm2 ships only the statically-linked `dmsetup.static` (no plain
# `dmsetup`), so symlink the conventional name onto PATH. Arch-independent (a
# symlink), so a plain tar layer for both arches. Carried for parity with the
# semgrep-scand image; the current fc-invoke warm-base restore path does not
# exec dmsetup, but the single base image supports every provisioner mode.
pkg_tar(
    name = "dmsetup_symlink_tar",
    symlinks = {"/usr/local/bin/dmsetup": "/sbin/dmsetup.static"},
)

# fc-invoke daemon container image. Built as an apko image (Wolfi base with
# dmsetup/lvm2 + util-linux) rather than the default distroless go_image, for
# parity with the semgrep-scand daemon it supersedes (both drive Firecracker
# guests as child processes and may manage device-mapper at runtime). The Go
# binary is cross-compiled per arch and layered at /usr/local/bin/fc-invoke
# (the apko entrypoint).

platform_transition_filegroup(
    name = "fc_invoke_amd64_bin",
    srcs = ["//projects/firecracker/substrate/invoke/cmd:invoke"],
    target_platform = "@rules_go//go/toolchain:linux_amd64",
)

tar(
    name = "fc_invoke_tar_amd64",
    srcs = [":fc_invoke_amd64_bin"],
    mtree = [
        "./usr/local/bin/fc-invoke type=file content=$(execpath :fc_invoke_amd64_bin) mode=0755 uid=0 gid=0",
    ],
)

platform_transition_filegroup(
    name = "fc_invoke_arm64_bin",
    srcs = ["//projects/firecracker/substrate/invoke/cmd:invoke"],
    target_platform = "@rules_go//go/toolchain:linux_arm64",
)

tar(
    name = "fc_invoke_tar_arm64",
    srcs = [":fc_invoke_arm64_bin"],
    mtree = [
        "./usr/local/bin/fc-invoke type=file content=$(execpath :fc_invoke_arm64_bin) mode=0755 uid=0 gid=0",
    ],
)

apko_image(
    name = "image",
    config = "apko.yaml",
    contents = "@fc_invoke_lock//:contents",
    # :fc_invoke_tar is the Go binary; @kata_firecracker//:tar bakes the
    # firecracker binary + guest kernel into /opt/fc so the node needs no
    # /opt/kata install. apko appends _amd64/_arm64 to each base per arch.
    multiarch_tars = [
        ":fc_invoke_tar",
        "@kata_firecracker//:tar",
    ],
    repository = "ghcr.io/jomcgi/homelab/projects/firecracker/substrate/invoke",
    tars = [":dmsetup_symlink_tar"],
)
