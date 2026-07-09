# gazelle:ignore
load("@aspect_bazel_lib//lib:tar.bzl", "tar")
load("@aspect_bazel_lib//lib:transitions.bzl", "platform_transition_filegroup")
load("//bazel/tools/oci:apko_image.bzl", "apko_image")

# The semgrep scanner microVM image: a Wolfi apko base with the offline-Pro
# scan-server engine (osemgrep-pro + co-located semgrep-core) and merged rule set
# layered in, and semgrep-guest-init as PID 1. The init keeps the scan-server
# (osemgrep-pro mcp --experimental --pro) warm — parsers warmed and rules compiled
# once — and serves scan requests over vsock. Engine path and rules dir are set in
# apko.yaml (OSEMGREP_PRO_BIN/SEMGREP_SCAN_RULES); the offline placeholder-token
# settings file and isolated HOME are written by the init at runtime.

# semgrep-guest-init (PID 1), cross-compiled per arch, layered at /usr/local/bin.
platform_transition_filegroup(
    name = "semgrep_guest_init_amd64_bin",
    srcs = ["//projects/firecracker/semgrep/guest-init/cmd"],
    target_platform = "@rules_go//go/toolchain:linux_amd64",
)

tar(
    name = "semgrep_guest_init_tar_amd64",
    srcs = [":semgrep_guest_init_amd64_bin"],
    mtree = [
        "./usr/local/bin/semgrep-guest-init type=file content=$(execpath :semgrep_guest_init_amd64_bin) mode=0755 uid=0 gid=0",
    ],
)

platform_transition_filegroup(
    name = "semgrep_guest_init_arm64_bin",
    srcs = ["//projects/firecracker/semgrep/guest-init/cmd"],
    target_platform = "@rules_go//go/toolchain:linux_arm64",
)

tar(
    name = "semgrep_guest_init_tar_arm64",
    srcs = [":semgrep_guest_init_arm64_bin"],
    mtree = [
        "./usr/local/bin/semgrep-guest-init type=file content=$(execpath :semgrep_guest_init_arm64_bin) mode=0755 uid=0 gid=0",
    ],
)

apko_image(
    name = "image",
    config = "apko.yaml",
    contents = "@semgrep_guest_lock//:contents",
    # Arch-specific layers: the Pro engine (per-arch GHCR artifact) and the
    # cross-compiled init binary. rules_tar is arch-independent.
    multiarch_tars = [
        "//bazel/semgrep/guest:engine_tar",
        ":semgrep_guest_init_tar",
    ],
    repository = "ghcr.io/jomcgi/homelab/projects/firecracker/semgrep/guest",
    tars = ["//bazel/semgrep/guest:rules_tar"],
)
