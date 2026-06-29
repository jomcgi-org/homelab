# gazelle:ignore
load("@aspect_bazel_lib//lib:tar.bzl", "tar")
load("@aspect_bazel_lib//lib:transitions.bzl", "platform_transition_filegroup")
load("@rules_pkg//pkg:tar.bzl", "pkg_tar")
load("//bazel/tools/oci:apko_image.bzl", "apko_image")

# Wolfi's lvm2 ships only the statically-linked `dmsetup.static` (no plain
# `dmsetup`), so symlink the conventional name onto PATH; the provisioner execs
# "dmsetup". Arch-independent (a symlink), so a plain tar layer for both arches.
pkg_tar(
    name = "dmsetup_symlink_tar",
    symlinks = {"/usr/local/bin/dmsetup": "/sbin/dmsetup.static"},
)

# Firecracker snapshot/restore controller (ADR 022) container image. Built as an
# apko image (Wolfi base with dmsetup/lvm2 + util-linux) rather than the default
# distroless go_image, because the controller manages device-mapper thin-pools at
# runtime for the ADR 026 copy-on-write rootfs. The Go binary is cross-compiled
# per arch and layered at /usr/local/bin/fc-agentd (the apko entrypoint).

platform_transition_filegroup(
    name = "fc_agentd_amd64_bin",
    srcs = ["//projects/agent_platform/fc-agentd/cmd"],
    target_platform = "@rules_go//go/toolchain:linux_amd64",
)

tar(
    name = "fc_agentd_tar_amd64",
    srcs = [":fc_agentd_amd64_bin"],
    mtree = [
        "./usr/local/bin/fc-agentd type=file content=$(execpath :fc_agentd_amd64_bin) mode=0755 uid=0 gid=0",
    ],
)

platform_transition_filegroup(
    name = "fc_agentd_arm64_bin",
    srcs = ["//projects/agent_platform/fc-agentd/cmd"],
    target_platform = "@rules_go//go/toolchain:linux_arm64",
)

tar(
    name = "fc_agentd_tar_arm64",
    srcs = [":fc_agentd_arm64_bin"],
    mtree = [
        "./usr/local/bin/fc-agentd type=file content=$(execpath :fc_agentd_arm64_bin) mode=0755 uid=0 gid=0",
    ],
)

apko_image(
    name = "image",
    config = "apko.yaml",
    contents = "@fc_agentd_lock//:contents",
    multiarch_tars = [":fc_agentd_tar"],
    repository = "ghcr.io/jomcgi/homelab/projects/agent_platform/fc-agentd",
    tars = [":dmsetup_symlink_tar"],
)
