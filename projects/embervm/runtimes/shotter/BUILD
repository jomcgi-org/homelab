# gazelle:ignore
load("@aspect_bazel_lib//lib:tar.bzl", "tar")
load("@aspect_bazel_lib//lib:transitions.bzl", "platform_transition_filegroup")
load("@rules_go//go:def.bzl", "go_library")
load("//bazel/tools/oci:apko_image.bzl", "apko_image")
load("//bazel/tools/pytest:defs.bzl", "py_test")

# Shotter task-class guest image (ADR embervm/035). Its PID 1 starts the vsock
# HTTP server, launches Chromium, polls the local CDP endpoint, settles, and
# only then flips /shim/ready. The first ready 200 is noded's base snapshot
# trigger, so every restored clone resumes with a live browser.

platform_transition_filegroup(
    name = "guest_init_amd64_bin",
    srcs = ["//projects/embervm/runtimes/shotter/guest-init/cmd"],
    target_platform = "@rules_go//go/toolchain:linux_amd64",
)

tar(
    name = "guest_init_tar_amd64",
    srcs = [
        "etc/hosts",
        "etc/shotter-egress.json",
        ":guest_init_amd64_bin",
    ],
    mtree = [
        "./usr/local/bin/ember-shotter-init type=file content=$(execpath :guest_init_amd64_bin) mode=0755 uid=0 gid=0",
        # /etc is read-only in the guest. Bake a local mapping for the hostname
        # guest-init installs before Chromium starts.
        "./etc/hosts type=file content=$(execpath etc/hosts) mode=0644 uid=0 gid=0",
        # The proxy's destination policy (host mapping plus allowlist). Baked
        # into the image rather than passed as an env var: a task-class guest
        # never receives one (see the comment on shotterEgressConfigPath in
        # proxy.go). Baking it also means the policy is covered by the image
        # digest instead of arriving from a caller-controlled or mutable
        # source (ADR embervm/035 section 4).
        "./etc/shotter-egress.json type=file content=$(execpath etc/shotter-egress.json) mode=0644 uid=0 gid=0",
    ],
)

apko_image(
    name = "image",
    # amd64 only. This must agree with apko.yaml's x86_64-only arch list and the
    # per-arch guest-init tar, otherwise image push can fail after PR CI passes.
    arm64 = False,
    config = "apko.yaml",
    contents = "@shotter_lock//:contents",
    repository = "ghcr.io/jomcgi/homelab/projects/embervm/runtimes/shotter",
    tars = [":guest_init_tar_amd64"],
)

# Embeds etc/shotter-egress.json so the guest-init Go test package can assert
# the checked-in baked policy itself parses and validates (issue #4994
# review round), not just a synthetic fixture with the same shape.
go_library(
    name = "egress_config",
    srcs = ["egress_config.go"],
    embedsrcs = ["etc/shotter-egress.json"],
    importpath = "github.com/jomcgi/homelab/projects/embervm/runtimes/shotter",
    visibility = ["//projects/embervm/runtimes/shotter/guest-init/cmd:__pkg__"],
)

# Cross-checks the baked egress policy (etc/shotter-egress.json) against the
# chart's global egress.internal.allowlist (issue #4994 review round, STPA
# pass): the two are independently hand-maintained with no other build-time
# link, so a future image rebuild could widen what the guest may dial with
# no chart-side signal at all. Mirrors
# projects/embervm/runtimes/claude/cleartext_lane_guard_test.py's pattern:
# read both files at test time rather than duplicating either.
# Hand-registered: gazelle does not generate py_test here.
py_test(
    name = "egress_policy_subset_test",
    srcs = ["egress_policy_subset_test.py"],
    data = [
        "etc/shotter-egress.json",
        "//projects/embervm/deploy:deploy_values",
    ],
    imports = ["."],
    deps = [
        "@pip//pyyaml",
    ],
)
