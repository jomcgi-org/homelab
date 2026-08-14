# gazelle:ignore
load("@aspect_bazel_lib//lib:tar.bzl", "tar")
load("@aspect_bazel_lib//lib:transitions.bzl", "platform_transition_filegroup")
load("//bazel/tools/oci:apko_image.bzl", "apko_image")

# EmberVM composite (R5) k3s-agent runtime image (ADR embervm/001, plan Task 3).
# Byte-identical to k3s-server in apko config and PID-1 init (../guest-init/cmd);
# the role is data (EMBER_GROUP_ROLE=agent) and the vendored airgap tarball is
# the same set. On a group cold boot ember-k3s-init runs `k3s agent --server
# https://$EMBER_PEER_SERVER:6443 --token $EMBER_GROUP_SECRET` (standing decision
# 13); health on 10250 (kubelet). For the Task 3 spike only the server is drilled
# to Ready; this image is built + test-covered but exercised in Task 5+. In-guest
# ROOT (see README.md). amd64 only.

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
    contents = "@embervm_runtime_k3s_agent_lock//:contents",
    repository = "ghcr.io/jomcgi/homelab/projects/embervm/runtimes/k3s/k3s-agent",
    tars = [
        "@k3s_archive//:tar_amd64",
        ":guest_init_tar_amd64",
    ],
)
