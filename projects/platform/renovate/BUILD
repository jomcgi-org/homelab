load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart")

helm_chart(
    name = "chart",
    visibility = ["//overlays:__subpackages__"],
)

argocd_app(
    name = "renovate",
    chart = "projects/platform/renovate",
    chart_files = ":chart",
    namespace = "monolith-workflows",
    release_name = "renovate",
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "values.yaml",
    ],
)
