load("//bazel/helm:defs.bzl", "argocd_app")

argocd_app(
    name = "kyverno",
    chart = "projects/platform/kyverno",
    chart_files = "//projects/platform/kyverno:chart",
    namespace = "kyverno",
    release_name = "kyverno",
    tags = [
        "helm",
        "template",
    ],
    values_files = ["//projects/platform/kyverno:values.yaml"],
)
