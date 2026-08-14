# gazelle:ignore
load("@aspect_bazel_lib//lib:tar.bzl", "tar")
load("@aspect_bazel_lib//lib:transitions.bzl", "platform_transition_filegroup")
load("//bazel/tools/oci:apko_image.bzl", "apko_image")

# EmberVM bazel-query demo runtime image (ADR embervm/010). A thin Wolfi apko
# base (busybox + a cc toolchain for bazel's local_config_cc probe) carrying the
# vendored Bazel 7.4.1 binary (/usr/local/bin/bazel, via @bazel_demo_bin) and the
# Abseil checkout + offline dep distdir (/opt/abseil + /opt/distdir, via
# @bazel_demo_workspace). Its PID 1 is ember-bazel-init (guest-init/cmd): on the
# base-build cold boot it runs the warming `bazel cquery //absl/...` fully offline
# (WORKSPACE mode + --distdir), and flips /shim/ready only AFTER the warming
# client exits, so BuildBase snapshots a warm bazel SERVER (the Skyframe analysis
# graph resident in the JVM heap). Restored clones resume inside the vsock serve
# loop and answer exactly one POST /query before Assign reaps the VM. In-guest
# ROOT (guest-init needs mounts; the microVM boundary is the isolation statement,
# see README.md). amd64 only, mirroring the k3s/postgres/python runtimes.

# ember-bazel-init, the PID-1 init: cross-compiled per arch and layered at
# /usr/local/bin/ember-bazel-init (the noded harnessInit boots it via init=<path>).
platform_transition_filegroup(
    name = "guest_init_amd64_bin",
    srcs = ["//projects/embervm/runtimes/bazel/guest-init/cmd"],
    target_platform = "@rules_go//go/toolchain:linux_amd64",
)

tar(
    name = "guest_init_tar_amd64",
    srcs = [
        "etc/hosts",
        ":guest_init_amd64_bin",
    ],
    mtree = [
        "./usr/local/bin/ember-bazel-init type=file content=$(execpath :guest_init_amd64_bin) mode=0755 uid=0 gid=0",
        # Bake /etc/hosts (127.0.0.1 localhost + the guest hostname) into the
        # read-only rootfs. /etc is read-only at boot so guest-init cannot write
        # it; the JVM's InetAddress.getLocalHost() resolves against this instead
        # of stalling on a lookup. Layered here (not via apko's base baselayout)
        # so the localhost + hostname mapping is explicit and versioned with the
        # runtime.
        "./etc/hosts type=file content=$(execpath etc/hosts) mode=0644 uid=0 gid=0",
    ],
)

apko_image(
    name = "image",
    # amd64 only: see the `arm64` arg in apko_image.bzl.
    arm64 = False,
    config = "apko.yaml",
    contents = "@embervm_runtime_bazel_lock//:contents",
    repository = "ghcr.io/jomcgi/homelab/projects/embervm/runtimes/bazel",
    tars = [
        # The vendored Bazel 7.4.1 binary (/usr/local/bin/bazel). @bazel_demo_bin
        # is a multiarch_http_file, whose //:tar alias resolves per arch.
        "@bazel_demo_bin//:tar_amd64",
        # The Abseil checkout (/opt/abseil) + the offline dep distdir
        # (/opt/distdir). Content is arch-independent; the rule emits identical
        # per-arch tars so multiarch_tars' label-suffixing works.
        "@bazel_demo_workspace//:tar_amd64",
        ":guest_init_tar_amd64",
    ],
)
