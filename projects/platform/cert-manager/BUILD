load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart")

helm_chart(
    name = "chart",
    lint = False,
    visibility = ["//visibility:public"],
)

argocd_app(
    name = "cert-manager",
    chart = "projects/platform/cert-manager",
    chart_files = ":chart",
    namespace = "cert-manager",
    release_name = "cert-manager",
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "values.yaml",
    ],
)
