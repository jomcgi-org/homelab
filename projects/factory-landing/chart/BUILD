load("//bazel/helm:defs.bzl", "helm_chart")
load("//bazel/tools/pytest:defs.bzl", "py_test")

helm_chart(
    name = "chart",
    images = {"image": "//projects/factory-landing:image.info"},
    publish = True,
    visibility = ["//visibility:public"],
)

py_test(
    name = "landing_test",
    srcs = ["landing_test.py"],
    data = [
        ":chart",
        "@multitool//tools/helm",
    ],
    env = {"HELM_BIN": "$(rootpath @multitool//tools/helm)"},
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)
