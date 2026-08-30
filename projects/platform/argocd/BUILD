load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart")

helm_chart(
    name = "chart",
    lint = False,
    visibility = ["//overlays:__subpackages__"],
)

argocd_app(
    name = "argocd",
    chart = "projects/platform/argocd",
    chart_files = ":chart",
    namespace = "argocd",
    release_name = "argocd",
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "values.yaml",
        "values-cluster.yaml",
    ],
)

argocd_app(
    name = "argocd-gke",
    generate_manifests = False,
    generate_semgrep = False,
    target_suffix = "_gke",
    chart = "projects/platform/argocd",
    chart_files = ":chart",
    namespace = "argocd",
    release_name = "argocd",
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "values.yaml",
        "values-gke.yaml",
    ],
)
