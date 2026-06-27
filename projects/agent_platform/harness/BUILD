# gazelle:ignore
load("@aspect_bazel_lib//lib:tar.bzl", "tar")
load("@aspect_bazel_lib//lib:transitions.bzl", "platform_transition_filegroup")
load("@rules_pkg//pkg:tar.bzl", "pkg_tar")
load("//bazel/tools/oci:apko_image.bzl", "apko_image")

# The microVM agent-harness image (ADR 022 Plan B): apko base (git/gh/go/node/
# pnpm from Wolfi) + the Goose binary + fc-agent-init as PID 1. fc-agent-init
# launches a turn-capped `goose run` whose between-turns boundary is the
# quiescent point the snapshot controller pauses on.

# Bake the Goose config at ~/.config/goose/config.yaml.
pkg_tar(
    name = "config_tar",
    srcs = ["config.yaml"],
    mode = "0644",
    owner = "65532.65532",
    package_dir = "/home/goose-agent/.config/goose",
)

# Bake the recipe library at ~/recipes (FC_GOOSE_RECIPE points here).
pkg_tar(
    name = "recipes_tar",
    srcs = glob(["recipes/*.yaml"]),
    mode = "0644",
    owner = "65532.65532",
    package_dir = "/home/goose-agent/recipes",
)

# fc-agent-init binary, cross-compiled per arch and layered at /usr/local/bin.
platform_transition_filegroup(
    name = "fc_agent_init_amd64_bin",
    srcs = ["//projects/agent_platform/fc-agent-init/cmd"],
    target_platform = "@rules_go//go/toolchain:linux_amd64",
)

tar(
    name = "fc_agent_init_tar_amd64",
    srcs = [":fc_agent_init_amd64_bin"],
    mtree = [
        "./usr/local/bin/fc-agent-init type=file content=$(execpath :fc_agent_init_amd64_bin) mode=0755 uid=0 gid=0",
    ],
)

platform_transition_filegroup(
    name = "fc_agent_init_arm64_bin",
    srcs = ["//projects/agent_platform/fc-agent-init/cmd"],
    target_platform = "@rules_go//go/toolchain:linux_arm64",
)

tar(
    name = "fc_agent_init_tar_arm64",
    srcs = [":fc_agent_init_arm64_bin"],
    mtree = [
        "./usr/local/bin/fc-agent-init type=file content=$(execpath :fc_agent_init_arm64_bin) mode=0755 uid=0 gid=0",
    ],
)

apko_image(
    name = "image",
    config = "apko.yaml",
    contents = "@harness_lock//:contents",
    multiarch_tars = [
        "@goose//:tar",
        ":fc_agent_init_tar",
    ],
    repository = "ghcr.io/jomcgi/homelab/projects/agent_platform/harness",
    tars = [
        ":config_tar",
        ":recipes_tar",
    ],
)
