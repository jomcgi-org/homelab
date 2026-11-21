load("//bazel/tools/pytest:defs.bzl", "py_test")

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
