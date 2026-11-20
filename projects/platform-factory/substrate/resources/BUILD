load("//bazel/tools/pytest:defs.bzl", "py_test")

exports_files(
    ["network-policy.yaml"],
    visibility = ["//projects/factory/chart:__pkg__"],
)

py_test(
    name = "resources_test",
    srcs = ["resources_test.py"],
    data = glob([
        "*.yaml",
        "patches/*.yaml",
        "upstream/**",
    ]) + [
        "//projects/platform-factory/substrate/credential-provider:image_digest",
        "@multitool//tools/kustomize",
    ],
    env = {
        "KUSTOMIZE_BIN": "$(rootpath @multitool//tools/kustomize)",
        "PROVIDER_DIGEST": "$(rootpath //projects/platform-factory/substrate/credential-provider:image_digest)",
    },
    deps = ["@pip//pyyaml"],
)
