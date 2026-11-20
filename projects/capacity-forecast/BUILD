# gazelle:ignore
load("@aspect_bazel_lib//lib:tar.bzl", "tar")
load("@aspect_rules_py//py/private/py_venv:defs.bzl", "py_venv_binary")
load("@rules_shell//shell:sh_test.bzl", "sh_test")
load("//bazel/tools/oci:py3_image.bzl", "py3_image")

exports_files([
    "requirements.txt",
    "timestral.BUILD",
])

py_venv_binary(
    name = "worker",
    srcs = ["worker.py"],
    main = "worker.py",
    target_compatible_with = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
    ],
    deps = [
        "@capacity_forecast_pip//numpy",
        "@capacity_forecast_pip//torch",
        "@timestral_source//:timestral",
    ],
)

tar(
    name = "model",
    srcs = [
        "@timestral_config//file",
        "@timestral_license//file",
        "@timestral_source//:LICENSE",
        "@timestral_weights//file",
    ],
    mtree = [
        "./model/config.json type=file content=$(execpath @timestral_config//file)",
        "./model/model.safetensors type=file content=$(execpath @timestral_weights//file)",
        "./model/LICENSE type=file content=$(execpath @timestral_license//file)",
        "./model/LICENSE.source type=file content=$(execpath @timestral_source//:LICENSE)",
    ],
)

py3_image(
    name = "image",
    # Ubuntu already resolves /bin/bash through /bin -> /usr/bin.
    bash_symlink = False,
    binary = ":worker",
    env = {"TIMESTRAL_MODEL_DIR": "/model"},
    tars = [":model"],
)

filegroup(
    name = "repo_docs",
    srcs = glob(["*.md"]),
    visibility = ["//projects/monolith:__pkg__"],
)

sh_test(
    name = "inference_test",
    srcs = ["inference_test.sh"],
    args = [
        "$(rootpath :image_image)",
    ],
    data = [
        ":image_image",
    ],
    # The smoke test needs the Linux runner's user namespaces, mount,
    # chroot and jq. The remote execution image does not supply these.
    tags = ["no-remote-exec"],
    target_compatible_with = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
    ],
)
