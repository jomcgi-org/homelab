# gazelle:ignore
load("@aspect_bazel_lib//lib:tar.bzl", "tar")
load("@aspect_bazel_lib//lib:transitions.bzl", "platform_transition_filegroup")
load("//bazel/tools/oci:apko_image.bzl", "apko_image")

platform_transition_filegroup(
    name = "receiver_amd64",
    srcs = ["//projects/factory/push-guard/receiver"],
    target_platform = "@rules_go//go/toolchain:linux_amd64",
)

tar(
    name = "receiver_files",
    srcs = [
        "seed-repository.sh",
        ":receiver_amd64",
        "//projects/factory/push-guard:pre-receive",
    ],
    mtree = [
        "./usr/local/bin/factory-push-guard type=file mode=0555 content=$(execpath :receiver_amd64)",
        "./usr/local/libexec/factory-push-guard/pre-receive type=file mode=0555 content=$(execpath //projects/factory/push-guard:pre-receive)",
        "./usr/local/libexec/factory-push-guard/seed-repository type=file mode=0555 content=$(execpath seed-repository.sh)",
    ],
)

apko_image(
    name = "image",
    arm64 = False,
    config = "apko.yaml",
    contents = "@factory_push_guard_lock//:contents",
    repository = "ghcr.io/jomcgi/homelab/projects/factory/push-guard",
    tars = [":receiver_files"],
)
