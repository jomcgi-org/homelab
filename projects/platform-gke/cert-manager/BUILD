load("//bazel/helm:defs.bzl", "argocd_app")

argocd_app(
    name = "cert-manager",
    chart = "projects/platform/cert-manager",
    chart_files = "//projects/platform/cert-manager:chart",
    namespace = "cert-manager",
    release_name = "cert-manager",
    tags = [
        "helm",
        "template",
    ],
    values_files = ["//projects/platform/cert-manager:values.yaml"],
)
