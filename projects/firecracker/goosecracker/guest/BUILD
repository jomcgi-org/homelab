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

# Recipe bodies are NOT baked into the image any more. The monolith runner
# injects them into the guest's /injected-context each turn (the same channel
# that delivers the router), so both fresh and snapshot-resumed threads run
# current recipe text. This filegroup is the single source of truth: the
# monolith takes it as data both for runtime injection (`//projects/monolith:main`)
# and for the recipe_catalog drift-guard test (which asserts the monolith's
# sub-recipe id manifest matches this checked-in set).
filegroup(
    name = "recipe_yamls",
    srcs = glob(["recipes/*.yaml"]),
    visibility = ["//projects/monolith:__subpackages__"],
)

# Generate /etc/environment.md from the apko lock plus hand-written notes
# (ADR agents/044), so the guest's self-description cannot drift from the
# packages actually baked into the image.
genrule(
    name = "environment_md",
    srcs = [
        "apko.lock.json",
        "env-notes.md",
    ],
    outs = ["environment.md"],
    cmd = "$(location //projects/firecracker/tools/env_readme:env_readme) --lock $(location apko.lock.json) --title 'goosecracker agent guest environment' --notes $(location env-notes.md) > $@",
    tools = ["//projects/firecracker/tools/env_readme"],
)

pkg_tar(
    name = "environment_md_tar",
    srcs = [":environment_md"],
    mode = "0644",
    owner = "0.0",
    package_dir = "/etc",
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
        ":environment_md_tar",
        ":impeccable_tar",
    ],
)
