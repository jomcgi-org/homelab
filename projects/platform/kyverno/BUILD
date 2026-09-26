load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart")

helm_chart(
    name = "chart",
    lint = False,
    visibility = ["//overlays:__subpackages__"],
)

argocd_app(
    name = "kyverno",
    chart = "projects/platform/kyverno",
    chart_files = ":chart",
    namespace = "kyverno",
    release_name = "kyverno",
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "values.yaml",
        "values-prod.yaml",
    ],
)
