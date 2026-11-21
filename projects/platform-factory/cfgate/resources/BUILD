load("//bazel/tools/pytest:defs.bzl", "py_test")

# The image audit renders this directory with kustomize
# (//projects/platform-factory/image-audit:platform_images_test).
filegroup(
    name = "audit_manifests",
    srcs = glob(
        ["**"],
        exclude = [
            "BUILD",
            "BUILD.bazel",
        ],
    ),
    visibility = ["//projects/platform-factory/image-audit:__pkg__"],
)

py_test(
    name = "secret_rbac_test",
    srcs = ["secret_rbac_test.py"],
    data = glob([
        "*.yaml",
        "upstream/*.yaml",
    ]) + ["@multitool//tools/kustomize"],
    env = {"KUSTOMIZE_BIN": "$(rootpath @multitool//tools/kustomize)"},
    deps = ["@pip//pyyaml"],
)
