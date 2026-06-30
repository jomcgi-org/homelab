# gazelle:ignore
load("@aspect_bazel_lib//lib:tar.bzl", "tar")
load("@aspect_bazel_lib//lib:transitions.bzl", "platform_transition_filegroup")
load("@rules_pkg//pkg:tar.bzl", "pkg_tar")
load("//bazel/tools/oci:apko_image.bzl", "apko_image")

# Wolfi's lvm2 ships only the statically-linked `dmsetup.static` (no plain
# `dmsetup`), so symlink the conventional name onto PATH; the devmapper
# provisioner execs "dmsetup". Arch-independent (a symlink), so a plain tar layer
# for both arches.
pkg_tar(
    name = "dmsetup_symlink_tar",
    symlinks = {"/usr/local/bin/dmsetup": "/sbin/dmsetup.static"},
)

# semgrep-scand daemon container image. Built as an apko image (Wolfi base with
# dmsetup/lvm2 + util-linux) rather than the default distroless go_image, because
# the daemon manages device-mapper thin-pools at runtime for the devmapper
# provisioner. The Go binary is cross-compiled per arch and layered at
# /usr/local/bin/semgrep-scand (the apko entrypoint).

platform_transition_filegroup(
    name = "semgrep_scand_amd64_bin",
    srcs = ["//projects/agent_platform/semgrep-scand/cmd"],
    target_platform = "@rules_go//go/toolchain:linux_amd64",
)

tar(
    name = "semgrep_scand_tar_amd64",
    srcs = [":semgrep_scand_amd64_bin"],
    mtree = [
        "./usr/local/bin/semgrep-scand type=file content=$(execpath :semgrep_scand_amd64_bin) mode=0755 uid=0 gid=0",
    ],
)

platform_transition_filegroup(
    name = "semgrep_scand_arm64_bin",
    srcs = ["//projects/agent_platform/semgrep-scand/cmd"],
    target_platform = "@rules_go//go/toolchain:linux_arm64",
)

tar(
    name = "semgrep_scand_tar_arm64",
    srcs = [":semgrep_scand_arm64_bin"],
    mtree = [
        "./usr/local/bin/semgrep-scand type=file content=$(execpath :semgrep_scand_arm64_bin) mode=0755 uid=0 gid=0",
    ],
)

apko_image(
    name = "image",
    config = "apko.yaml",
    contents = "@semgrep_scand_lock//:contents",
    multiarch_tars = [":semgrep_scand_tar"],
    repository = "ghcr.io/jomcgi/homelab/projects/agent_platform/semgrep-scand",
    tars = [":dmsetup_symlink_tar"],
)
