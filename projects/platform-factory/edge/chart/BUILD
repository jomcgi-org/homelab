load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart")
load("//bazel/tools/pytest:defs.bzl", "py_test")

helm_chart(
    name = "chart",
    lint = False,
    visibility = ["//visibility:public"],
)

argocd_app(
    name = "edge",
    chart = "projects/platform-factory/edge/chart",
    chart_files = ":chart",
    namespace = "edge",
    release_name = "edge",
    tags = [
        "helm",
        "template",
    ],
    values_files = ["values.yaml"],
)

py_test(
    name = "edge_test",
    srcs = ["edge_test.py"],
    data = [
        ":chart",
        "@multitool//tools/helm",
    ],
    env = {"HELM_BIN": "$(rootpath @multitool//tools/helm)"},
    deps = ["@pip//pyyaml"],
)
