# gazelle:ignore
load("@aspect_bazel_lib//lib:tar.bzl", "tar")
load("@aspect_bazel_lib//lib:transitions.bzl", "platform_transition_filegroup")
load("//bazel/tools/oci:apko_image.bzl", "apko_image")

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
        ":guest_init_amd64_bin",
    ],
    mtree = [
        "./usr/local/bin/ember-shotter-init type=file content=$(execpath :guest_init_amd64_bin) mode=0755 uid=0 gid=0",
        # /etc is read-only in the guest. Bake a local mapping for the hostname
        # guest-init installs before Chromium starts.
        "./etc/hosts type=file content=$(execpath etc/hosts) mode=0644 uid=0 gid=0",
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
