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
        "//projects/platform-factory/edge-admission/resources:policies.yaml",
        "//projects/platform-factory/edge-tunnel/resources:route.yaml",
        "@multitool//tools/helm",
    ],
    env = {
        "EDGE_ADMISSION": "$(rootpath //projects/platform-factory/edge-admission/resources:policies.yaml)",
        "EDGE_TUNNEL": "$(rootpath //projects/platform-factory/edge-tunnel/resources:route.yaml)",
        "HELM_BIN": "$(rootpath @multitool//tools/helm)",
    },
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)
