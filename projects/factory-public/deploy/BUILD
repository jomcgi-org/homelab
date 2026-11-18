load("//bazel/helm:defs.bzl", "argocd_app")

exports_files(["application.yaml"])

argocd_app(
    name = "factory-public",
    chart = "projects/factory-public/chart",
    chart_files = "//projects/factory-public/chart:chart",
    namespace = "factory-public",
    release_name = "factory-public",
    tags = [
        "helm",
        "template",
    ],
    values_files = ["//projects/factory-public/chart:values.yaml"],
)
