load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart")

helm_chart(
    name = "chart",
    lint = False,
    visibility = ["//overlays:__subpackages__"],
)

argocd_app(
    name = "keda",
    chart = "projects/platform/keda",
    chart_files = ":chart",
    namespace = "keda",
    release_name = "keda",
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "values.yaml",
    ],
)
