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

# Expose the recipe YAMLs (read-only) to the monolith's recipe_catalog
# drift-guard test, which asserts the monolith's sub-recipe id manifest
# matches this checked-in set. Same source glob as recipes_tar above; this
# just adds a public label so a cross-package py_test can take it as data.
filegroup(
    name = "recipe_yamls",
    srcs = glob(["recipes/*.yaml"]),
    visibility = ["//projects/monolith:__subpackages__"],
)

# Bake impeccable's frontend-design reference docs at /opt/impeccable/. The
# artifact-build / artifact-review recipes consult these on demand for depth on
# a specific dimension. Vendored unmodified (Apache-2.0); NOTICE.md ships with
# them to carry attribution in the distributed image (see impeccable/LICENSE).
pkg_tar(
    name = "impeccable_tar",
    srcs = glob(["vendor/impeccable/*.md"]),
    mode = "0644",
    owner = "65532.65532",
    package_dir = "/opt/impeccable",
    strip_prefix = "vendor/impeccable",
)

# fc-agent-init binary, cross-compiled per arch and layered at /usr/local/bin.
platform_transition_filegroup(
    name = "fc_agent_init_amd64_bin",
    srcs = ["//projects/firecracker/goosecracker/guest-init/cmd"],
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
    srcs = ["//projects/firecracker/goosecracker/guest-init/cmd"],
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
    repository = "ghcr.io/jomcgi/homelab/projects/firecracker/goosecracker/guest",
    tars = [
        ":config_tar",
        ":recipes_tar",
        ":impeccable_tar",
    ],
)
