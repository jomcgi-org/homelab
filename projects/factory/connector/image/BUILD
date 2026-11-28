# gazelle:ignore
load("@aspect_bazel_lib//lib:tar.bzl", "tar")
load("@aspect_bazel_lib//lib:transitions.bzl", "platform_transition_filegroup")
load("//bazel/tools/oci:apko_image.bzl", "apko_image")
load("//projects/factory/discord/libs:libs.bzl", "LIBS")

platform_transition_filegroup(
    name = "connector_amd64_bin",
    srcs = ["//projects/factory/connector/cmd/connector"],
    target_platform = "@rules_go//go/toolchain:linux_amd64",
)

# The Discord bridge (projects/factory/discord) ships in this image too and
# runs with its own command, so it needs no image of its own.
platform_transition_filegroup(
    name = "discord_bridge_amd64_bin",
    srcs = ["//projects/factory/discord/cmd/discord-bridge"],
    target_platform = "@rules_go//go/toolchain:linux_amd64",
)

# So does Bosun's artifact MCP server (projects/factory/discord/artifacts).
platform_transition_filegroup(
    name = "bosun_artifacts_amd64_bin",
    srcs = ["//projects/factory/discord/cmd/bosun-artifacts"],
    target_platform = "@rules_go//go/toolchain:linux_amd64",
)

# And its history MCP server (projects/factory/discord/archive).
platform_transition_filegroup(
    name = "bosun_history_amd64_bin",
    srcs = ["//projects/factory/discord/cmd/bosun-history"],
    target_platform = "@rules_go//go/toolchain:linux_amd64",
)

# And the Job that uploads the pinned artifact libraries
# (projects/factory/discord/libs), with the files it uploads.
platform_transition_filegroup(
    name = "bosun_libs_amd64_bin",
    srcs = ["//projects/factory/discord/cmd/bosun-libs"],
    target_platform = "@rules_go//go/toolchain:linux_amd64",
)

tar(
    name = "connector_tar_amd64",
    srcs = [
        ":bosun_artifacts_amd64_bin",
        ":bosun_history_amd64_bin",
        ":bosun_libs_amd64_bin",
        ":connector_amd64_bin",
        ":discord_bridge_amd64_bin",
    ] + LIBS.values(),
    mtree = [
        "./usr/local/bin/connector type=file content=$(execpath :connector_amd64_bin) mode=0755 uid=0 gid=0",
        "./usr/local/bin/discord-bridge type=file content=$(execpath :discord_bridge_amd64_bin) mode=0755 uid=0 gid=0",
        "./usr/local/bin/bosun-artifacts type=file content=$(execpath :bosun_artifacts_amd64_bin) mode=0755 uid=0 gid=0",
        "./usr/local/bin/bosun-history type=file content=$(execpath :bosun_history_amd64_bin) mode=0755 uid=0 gid=0",
        "./usr/local/bin/bosun-libs type=file content=$(execpath :bosun_libs_amd64_bin) mode=0755 uid=0 gid=0",
    ] + [
        "./usr/share/bosun-libs/%s type=file content=$(execpath %s) mode=0644 uid=0 gid=0" % (name, label)
        for name, label in LIBS.items()
    ],
)

apko_image(
    name = "image",
    # amd64 only: see the `arm64` arg in apko_image.bzl.
    arm64 = False,
    config = "apko.yaml",
    contents = "@factory_connector_lock//:contents",
    repository = "ghcr.io/jomcgi/homelab/projects/factory/connector",
    tars = [
        ":connector_tar_amd64",
    ],
)
