# gazelle:ignore
load("@aspect_bazel_lib//lib:tar.bzl", "tar")
load("@aspect_bazel_lib//lib:transitions.bzl", "platform_transition_filegroup")
load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/tools/oci:apko_image.bzl", "apko_image")
load("//bazel/tools/pytest:defs.bzl", "py_test")

# EmberVM claude runtime base image (issue #4055). A session-class guest whose
# PID 1 is a stdlib-python shim that owns a long-lived Claude Code CLI process
# and answers the frozen HTTP-over-vsock guest contract on port 1027
# (vsockproto.GuestHTTPPort). One HTTP request drives one agent turn; the CLI
# process survives between turns so warm turns cost ~80ms of local overhead
# instead of ~250ms for a cold spawn.

# The shim as an importable library so the unit test can `import shim` without
# depending on the tar-packaging target. Stdlib only: the guest image carries
# python-3.12 and no pip packages, so the shim must not grow third-party imports.
py_library(
    name = "shim_lib",
    srcs = ["shim.py"],
    imports = ["."],
)

# The shim, layered at /usr/local/bin/ember-claude-shim (the apko entrypoint runs
# python3 against it). Platform-independent python source.
tar(
    name = "shim_tar",
    srcs = ["shim.py"],
    mtree = [
        "./usr/local/bin/ember-claude-shim type=file content=$(execpath shim.py) mode=0755 uid=0 gid=0",
    ],
)

# Workspace trust pre-seed. Remote Control server mode and the interactive paths
# refuse an untrusted workspace ("Error: Workspace not trusted") and the dialog is
# interactive, so a fresh guest with nobody at a keyboard would wedge on first
# boot. The record is keyed by ABSOLUTE workspace path and must match
# EMBER_CLAUDE_WORKSPACE in apko.yaml exactly.
#
# Baked under /usr/share, NOT at its final ~/.claude.json: the rootfs is read-only
# on every boot, so guest-init bind-mounts a writable HOME over /home/runtime, and
# a file baked underneath that mount point would be hidden the moment it lands.
# guest-init copies this master into the writable HOME when none is already there
# (trustRecordSeedPath in guest-init/cmd/volume_linux.go).
tar(
    name = "guest_config_tar",
    srcs = ["guest-config/claude.json"],
    mtree = [
        "./usr/share/ember-claude/claude.json type=file content=$(execpath guest-config/claude.json) mode=0644 uid=0 gid=0",
    ],
)

apko_image(
    name = "image",
    # amd64-only: the Claude Code CLI is a single 262 MB arch-specific ELF and
    # apko.yaml's archs list is x86_64-only to match, so :image is a
    # single-platform amd64 image rather than a multi-arch index. Without this,
    # the arm64 variant fails SBOM generation with "no architecture images
    # found" because the config declares no aarch64.
    arm64 = False,
    config = "apko.yaml",
    contents = "@embervm_runtime_claude_lock//:contents",
    repository = "ghcr.io/jomcgi/homelab/projects/embervm/runtimes/claude",
    tars = [
        ":shim_tar",
        ":guest_config_tar",
        ":guest_init_tar_amd64",
        # The Claude Code CLI, a single 262 MB glibc ELF extracted from the npm
        # registry tarball (see claude_code_cli in MODULE.bazel). amd64 only, so
        # this is the concrete per-arch tar rather than a multiarch_tars entry.
        "@claude_code_cli//:tar_amd64",
        # The Codex CLI, a statically linked musl binary from the GitHub release
        # (see codex_cli in MODULE.bazel). It drives the Luna/Terra/Sol models
        # through the same shim, spawn-per-turn with `exec resume` rather than
        # the claude CLI's long-lived stream-json REPL (#4234).
        "@codex_cli//:tar_amd64",
        # The pi coding agent, a self-contained ELF from the GitHub release (see
        # pi_coding_agent in MODULE.bazel). It drives Qwen against the in-cluster
        # vLLM endpoint, chosen over the claude CLI because its 4 tools and
        # replaceable system prompt fit Qwen's 32k window (#4234).
        "@pi_coding_agent//:tar_amd64",
    ],
)

# The shim is plain stdlib python, unit-tested without a microVM or vsock (a
# loopback TCP server stands in for the vsock listener), and without a real CLI
# (a fake subprocess emitting recorded stream-json stands in). Hand-registered:
# gazelle does not generate py_test targets in this repo.
py_test(
    name = "shim_test",
    srcs = ["shim_test.py"],
    imports = ["."],
    deps = [
        ":shim_lib",
        "@pip//pytest",
    ],
)

py_test(
    name = "shim_hydration_test",
    srcs = ["shim_hydration_test.py"],
    imports = ["."],
    deps = [
        ":shim_lib",
        "@pip//pytest",
    ],
)

platform_transition_filegroup(
    name = "guest_init_amd64_bin",
    srcs = ["//projects/embervm/runtimes/claude/guest-init/cmd"],
    target_platform = "@rules_go//go/toolchain:linux_amd64",
)

tar(
    name = "guest_init_tar_amd64",
    srcs = [":guest_init_amd64_bin"],
    mtree = [
        "./usr/local/bin/ember-runtime-guest-init type=file content=$(execpath :guest_init_amd64_bin) mode=0755 uid=0 gid=0",
    ],
)

# Guard (ADR 023 6b): the guest points its API client at an http:// URL, so the
# sidecar MUST have a credential entry for that host. Without one the connection
# blind-tunnels and the whole prompt leaves the cluster unencrypted.
# Hand-registered: gazelle does not generate py_test here.
py_test(
    name = "cleartext_lane_guard_test",
    srcs = ["cleartext_lane_guard_test.py"],
    data = [
        "//projects/embervm/deploy:deploy_values",
        "//projects/embervm/runtimes/claude/guest-init/cmd:guest_init_source",
    ],
    imports = ["."],
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)
