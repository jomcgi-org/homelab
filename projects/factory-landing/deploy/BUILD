load("//bazel/helm:defs.bzl", "argocd_app")

argocd_app(
    name = "factory-landing",
    chart = "projects/factory-landing/chart",
    chart_files = "//projects/factory-landing/chart:chart",
    namespace = "factory-landing",
    release_name = "factory-landing",
    tags = ["helm", "template"],
    values_files = ["//projects/factory-landing/chart:values.yaml"],
)
