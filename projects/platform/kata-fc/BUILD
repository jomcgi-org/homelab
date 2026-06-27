load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart")

helm_chart(
    name = "chart",
    visibility = ["//overlays:__subpackages__"],
)

argocd_app(
    name = "kata-fc",
    chart = "projects/platform/kata-fc",
    chart_files = ":chart",
    namespace = "default",
    release_name = "kata-fc",
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "values.yaml",
    ],
)
