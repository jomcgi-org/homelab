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
    chart = "projects/platform/argocd",
    chart_files = ":chart",
    generate_manifests = False,
    generate_semgrep = False,
    namespace = "argocd",
    release_name = "argocd",
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
