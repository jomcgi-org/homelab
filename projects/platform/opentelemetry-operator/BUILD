load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart")

helm_chart(
    name = "chart",
    lint = False,
    visibility = ["//overlays:__subpackages__"],
)

argocd_app(
    name = "opentelemetry-operator",
    chart = "projects/platform/opentelemetry-operator",
    chart_files = ":chart",
    namespace = "opentelemetry-operator",
    release_name = "opentelemetry-operator",
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "values.yaml",
        "values-prod.yaml",
    ],
)
