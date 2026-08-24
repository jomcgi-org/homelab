# gazelle:ignore
load("@aspect_bazel_lib//lib:tar.bzl", "tar")
load("@aspect_bazel_lib//lib:transitions.bzl", "platform_transition_filegroup")
load("@aspect_rules_py//py:defs.bzl", "py_library")
load("@rules_shell//shell:sh_test.bzl", "sh_test")
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
    # The pi runtime image ships this SAME artifact rather than rebuilding its
    # own, so the two guests can never drift in the shim. Same reasoning for
    # guest_config_tar and guest_init_tar_amd64 below.
    visibility = ["//projects/embervm/runtimes/pi:__pkg__"],
)

# The apko config as a data dependency, so the pi runtime's package-parity guard
# can read this image's package list. Exported rather than duplicated: the guard
# exists precisely to catch the two lists drifting.
filegroup(
    name = "apko_yaml",
    srcs = ["apko.yaml"],
    visibility = ["//projects/embervm/runtimes/pi:__pkg__"],
)

# The CLI reads its source bundle at runtime, so patch it as a Bazel artifact
# rather than mutating the downloaded external repository or an installed CLI.
genrule(
    name = "claude_code_patched_binary_amd64",
    srcs = [
        "@claude_code_cli//:amd64",
        "//tools/claude-code-patch:patch_claude.py",
    ],
    outs = ["claude_code_patched_amd64"],
    cmd = "python3 $(location //tools/claude-code-patch:patch_claude.py) " +
          "$(location @claude_code_cli//:amd64) $@ && chmod 0755 $@",
    tools = ["//tools/claude-code-patch:patch_claude.py"],
)

genrule(
    name = "claude_code_patched_tar_amd64",
    srcs = [":claude_code_patched_binary_amd64"],
    outs = ["claude_code_patched_tar_amd64.tar"],
    cmd = "mkdir -p tmp/usr/local/bin && " +
          "cp $< tmp/usr/local/bin/claude && " +
          "tar -C tmp -czf $@ .",
)

sh_test(
    name = "claude_code_late_resume_smoke_test",
    srcs = ["late_resume_smoke_test.sh"],
    args = ["$(rootpath :claude_code_patched_binary_amd64)"],
    data = [":claude_code_patched_binary_amd64"],
    target_compatible_with = ["@platforms//os:linux"],
)

# Workspace trust pre-seed. Remote Control server mode and the interactive paths
# refuse an untrusted workspace ("Error: Workspace not trusted") and the dialog is
# interactive, so a fresh guest with nobody at a keyboard would wedge on first
# boot. The record trusts both ABSOLUTE workspace paths: /workspace is the
# default workspace, while /workspace/src is the checkout path used for
# hydration.
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
    # Shared with the pi runtime image, which carries no claude CLI but still
    # needs this file: guest-init's seedTrustRecord reads it UNCONDITIONALLY and
    # errors if it is absent (volume_linux.go), so a pi guest without it never
    # finishes boot.
    visibility = ["//projects/embervm/runtimes/pi:__pkg__"],
)

# The CLIs this image prewarms at shim boot (#4423), read by the shared shim's
# ProcessManager._read_prewarm_clis. This image carries all three families, so
# a base build parks a claude CLI, a codex app-server and a pi RPC process in
# the snapshot and every family's first turn adopts instead of lazy-spawning.
#
# This file, not initEnv, is how the list reaches the guest: task-class guests
# have no env delivery (#4429), BuildBaseRequest.init_env is dead weight. The
# pi runtime bakes its own tar with only "pi" at this exact path, because its
# image omits the other two CLIs and prewarming them would fail the boot.
tar(
    name = "prewarm_clis_tar",
    srcs = ["prewarm-clis"],
    mtree = [
        "./usr/share/ember-shim/prewarm-clis type=file content=$(execpath prewarm-clis) mode=0644 uid=0 gid=0",
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
        ":prewarm_clis_tar",
        ":guest_init_tar_amd64",
        # The Claude Code CLI, a single 262 MB glibc ELF extracted from the npm
        # registry tarball (see claude_code_cli in MODULE.bazel). amd64 only, so
        # this is the concrete per-arch tar rather than a multiarch_tars entry.
        ":claude_code_patched_tar_amd64",
        # The Codex CLI, a statically linked musl binary from the GitHub release
        # (see codex_cli in MODULE.bazel). It drives the Luna/Terra/Sol models
        # through the same shim, spawn-per-turn with `exec resume` rather than
        # the claude CLI's long-lived stream-json REPL (#4234).
        "@codex_cli//:tar_amd64",
        # The pi coding agent, a self-contained ELF from the GitHub release (see
        # pi_coding_agent in MODULE.bazel). It drives Qwen against the in-cluster
        # vLLM endpoint, chosen over the claude CLI because its small tool set
        # and replaceable system prompt fit Qwen's context window (#4234).
        "@pi_coding_agent//:tar_amd64",
        # Pi's search and page-fetch tools. This path must exist in both runtime
        # images because the shared shim can route qwen through either one.
        "//projects/embervm/runtimes/pi:web_research_extension_tar",
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

# Guard: pi must be told Qwen's real context window. A stale value re-arms
# the 400 that killed guest turns (see PI_CONTEXT_WINDOW in shim.py).
# Hand-registered: gazelle does not generate py_test here.
py_test(
    name = "pi_context_window_sync_test",
    srcs = ["pi_context_window_sync_test.py"],
    data = ["//projects/inference/deploy:values_yaml"],
    imports = ["."],
    deps = [
        ":shim_lib",
        "@pip//pytest",
        "@pip//pyyaml",
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
    # Shared with the pi runtime image. Both workloads name the same
    # harnessInit path (/usr/local/bin/ember-runtime-guest-init) in the chart's
    # `workloads` table, so they must be the same binary.
    visibility = ["//projects/embervm/runtimes/pi:__pkg__"],
)

# Guard (#4458): the ENOSPC reclaim deletes base bundles on a wedged brick, so
# it must ship disarmed and its stop condition must be able to evaluate. Reads
# the chart via a cross-package data dep because a chart package has no Python
# toolchain context.
# Hand-registered: gazelle does not generate py_test here.
py_test(
    name = "rootfs_reclaim_guard_test",
    srcs = ["rootfs_reclaim_guard_test.py"],
    data = [
        "//projects/embervm/chart:rootfs_reclaim_sources",
        "//projects/embervm/deploy:deploy_values",
    ],
    imports = ["."],
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
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
