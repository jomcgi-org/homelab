# gazelle:ignore
load("@aspect_bazel_lib//lib:tar.bzl", "tar")
load("@aspect_bazel_lib//lib:transitions.bzl", "platform_transition_filegroup")
load("//bazel/tools/oci:apko_image.bzl", "apko_image")

# EmberVM composite (R5) k3s-server runtime image (ADR embervm/001, plan Task 3).
# A thin Wolfi apko base (busybox + iptables + the k3s shell-out tools) carrying
# the vendored static k3s binary and the airgap image tarball (via the
# @k3s_archive layer), whose PID 1 is ember-k3s-init (../guest-init/cmd). On a
# serving-class cold boot (the Task 3 single-VM spike) it decodes the injected
# EMBER_GROUP_* facts, starts the vsock clock agent, and runs `k3s server` with
# the sqlite datastore, flannel host-gw, the group secret as the cluster token,
# and a static token-auth API entry (standing decision 13). Health surfaces on
# 6443. In-guest ROOT (k3s needs it; the microVM boundary is the isolation
# statement, see README.md). amd64 only, mirroring the postgres/python runtimes.

# ember-k3s-init, the PID-1 init: cross-compiled per arch and layered at
# /usr/local/bin/ember-k3s-init (the noded harnessInit boots it via init=<path>).
platform_transition_filegroup(
    name = "guest_init_amd64_bin",
    srcs = ["//projects/embervm/runtimes/k3s/guest-init/cmd"],
    target_platform = "@rules_go//go/toolchain:linux_amd64",
)

tar(
    name = "guest_init_tar_amd64",
    srcs = [":guest_init_amd64_bin"],
    mtree = [
        "./usr/local/bin/ember-k3s-init type=file content=$(execpath :guest_init_amd64_bin) mode=0755 uid=0 gid=0",
    ],
)

apko_image(
    name = "image",
    # amd64 only: see the `arm64` arg in apko_image.bzl.
    arm64 = False,
    config = "apko.yaml",
    contents = "@embervm_runtime_k3s_server_lock//:contents",
    repository = "ghcr.io/jomcgi/homelab/projects/embervm/runtimes/k3s/k3s-server",
    tars = [
        # The vendored k3s binary (/usr/local/bin/k3s) + the airgap tarball
        # (/var/lib/rancher/k3s/agent/images/...), per arch (@k3s_archive//:tar
        # resolves to :tar_amd64 / :tar_arm64).
        "@k3s_archive//:tar_amd64",
        ":guest_init_tar_amd64",
    ],
)
