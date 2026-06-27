load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart")

helm_chart(
    name = "chart",
    visibility = ["//overlays:__subpackages__"],
)

argocd_app(
    name = "priority-classes",
    chart = "projects/platform/priority-classes",
    chart_files = ":chart",
    namespace = "kube-system",
    release_name = "priority-classes",
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "values.yaml",
    ],
)
