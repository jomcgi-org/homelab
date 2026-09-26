load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart")

helm_chart(
    name = "chart",
    lint = False,
    visibility = ["//overlays:__subpackages__"],
)

argocd_app(
    name = "argo-workflows",
    chart = "projects/platform/argo-workflows",
    chart_files = ":chart",
    namespace = "monolith-workflows",
    release_name = "argo-workflows",
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "values.yaml",
    ],
)

argocd_app(
    name = "argo-workflows-gke",
    chart = "projects/platform/argo-workflows",
    chart_files = ":chart",
    namespace = "monolith-workflows",
    release_name = "argo-workflows",
    tags = [
        "helm",
        "template",
    ],
    target_suffix = "_gke",
    values_files = [
        "values.yaml",
        "values-gke.yaml",
    ],
)
