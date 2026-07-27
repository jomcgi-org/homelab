# gazelle:ignore
load("@aspect_bazel_lib//lib:tar.bzl", "tar")
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
tar(
    name = "guest_config_tar",
    srcs = ["guest-config/claude.json"],
    mtree = [
        "./home/runtime/.claude.json type=file content=$(execpath guest-config/claude.json) mode=0644 uid=65532 gid=65532",
    ],
)

apko_image(
    name = "image",
    config = "apko.yaml",
    contents = "@embervm_runtime_claude_lock//:contents",
    repository = "ghcr.io/jomcgi/homelab/projects/embervm/runtimes/claude",
    tars = [
        ":shim_tar",
        ":guest_config_tar",
        # The Claude Code CLI, a single 262 MB glibc ELF extracted from the npm
        # registry tarball (see claude_code_cli in MODULE.bazel). amd64 only, so
        # this is the concrete per-arch tar rather than a multiarch_tars entry.
        "@claude_code_cli//:tar_amd64",
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
