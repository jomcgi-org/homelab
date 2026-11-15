load("//bazel/tools/pytest:defs.bzl", "py_test")

py_test(
    name = "library_test",
    srcs = ["library_test.py"],
    data = [
        "//projects/shared/helm/factory-library/chart",
        "@multitool//tools/helm",
    ] + glob(["goldens/**"]),
    env = {"HELM_BIN": "$(rootpath @multitool//tools/helm)"},
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)
