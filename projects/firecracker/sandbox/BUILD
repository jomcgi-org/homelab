# gazelle:ignore
load("@aspect_bazel_lib//lib:tar.bzl", "tar")
load("@aspect_bazel_lib//lib:transitions.bzl", "platform_transition_filegroup")

# The single sandbox-guest-init binary, shared by all six language guest images
# (ADR agents/057). One binary rather than one per language: the language is a
# property of the IMAGE, selected at boot from the /etc/sandbox-language file
# each image bakes, so the code is identical across them and only the toolchain
# packages and that one-line file differ.
#
# Lives in this parent package so the six sibling image packages layer the same
# built artifact instead of each transitioning and taring their own copy.
platform_transition_filegroup(
    name = "guest_init_amd64_bin",
    srcs = ["//projects/firecracker/sandbox/guest-init/cmd"],
    target_platform = "@rules_go//go/toolchain:linux_amd64",
)

tar(
    name = "guest_init_tar_amd64",
    srcs = [":guest_init_amd64_bin"],
    mtree = [
        "./usr/local/bin/sandbox-guest-init type=file content=$(execpath :guest_init_amd64_bin) mode=0755 uid=0 gid=0",
    ],
    visibility = ["//projects/firecracker/sandbox:__subpackages__"],
)
