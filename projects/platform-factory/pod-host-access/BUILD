load("//bazel/tools/pytest:defs.bzl", "py_test")

py_test(
    name = "host_access_test",
    srcs = ["host_access_test.py"],
    data = [
        "resources/kustomization.yaml",
        "resources/policies.yaml",
        "@multitool//tools/kustomize",
    ],
    env = {"KUSTOMIZE_BIN": "$(rootpath @multitool//tools/kustomize)"},
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)
