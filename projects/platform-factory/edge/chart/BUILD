load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart")

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
