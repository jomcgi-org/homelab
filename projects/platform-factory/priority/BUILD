load("//bazel/tools/pytest:defs.bzl", "py_test")

py_test(
    name = "priority_test",
    srcs = ["priority_test.py"],
    data = [
        "priority-class.yaml",
        "//projects/platform-factory/edge/chart",
        "//projects/platform-factory/factory-db/resources:cluster.yaml",
        "//projects/platform-factory/garage/resources:garage.yaml",
        "//projects/platform-factory/kagent-postgres/resources:cluster.yaml",
        "//projects/platform/authentik:chart",
        "//projects/platform/onepassword:chart",
        "@multitool//tools/helm",
    ],
    env = {"HELM_BIN": "$(rootpath @multitool//tools/helm)"},
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)
