load("//bazel/helm:defs.bzl", "argocd_app")

argocd_app(
    name = "kargo",
    chart = "projects/platform/kargo",
    chart_files = "//projects/platform/kargo:chart",
    namespace = "kargo",
    release_name = "kargo",
    tags = [
        "helm",
        "template",
    ],
    values_files = ["//projects/platform/kargo:values.yaml"],
)
