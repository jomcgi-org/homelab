# gazelle:ignore
load("@aspect_bazel_lib//lib:tar.bzl", "tar")
load("@aspect_bazel_lib//lib:transitions.bzl", "platform_transition_filegroup")
load("//bazel/tools/oci:apko_image.bzl", "apko_image")
load("//bazel/tools/pytest:defs.bzl", "py_test")

# amd64 daemon image on a Wolfi base. The executable is layered at
# /usr/local/bin/embervm-noded; the Firecracker bundle supplies firecracker,
# jailer, snapshot-editor and the guest kernel at /opt/fc.

platform_transition_filegroup(
    name = "noded_amd64_bin",
    srcs = ["//projects/embervm/noded/cmd:noded"],
    target_platform = "@rules_go//go/toolchain:linux_amd64",
)

tar(
    name = "noded_tar_amd64",
    srcs = [":noded_amd64_bin"],
    mtree = [
        "./usr/local/bin/embervm-noded type=file content=$(execpath :noded_amd64_bin) mode=0755 uid=0 gid=0",
    ],
)

apko_image(
    name = "image",
    arm64 = False,
    config = "apko.yaml",
    contents = "@embervm_noded_lock//:contents",
    repository = "ghcr.io/jomcgi/homelab/projects/embervm/noded/image",
    tars = [
        ":noded_tar_amd64",
        "@kata_firecracker//:tar_amd64",
    ],
)

py_test(
    name = "image_contents_test",
    srcs = ["image_contents_test.py"],
    data = [
        "apko.lock.json",
        "apko.yaml",
        ":image",
    ],
    env = {"NODED_IMAGE": "$(rootpath :image)"},
    deps = ["@pip//pytest"],
)
