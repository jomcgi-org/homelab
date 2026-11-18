load("//bazel/tools/pytest:defs.bzl", "py_test")

py_test(
    name = "kube_access_test",
    srcs = ["kube_access_test.py"],
    data = [
        "admins.yaml",
        "viewers.yaml",
    ],
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)
