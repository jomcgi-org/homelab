load("//bazel/tools/pytest:defs.bzl", "py_test")

py_test(
    name = "kube_access_test",
    srcs = ["kube_access_test.py"],
    data = [
        "admins.yaml",
        "explorers.yaml",
        "kustomization.yaml",
        "viewers.yaml",
        "//projects:factory-cluster/nodes/authentication-config.yaml",
    ],
    deps = ["@pip//pyyaml"],
)
