load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart")

helm_chart(
    name = "chart",
    lint = False,
    visibility = ["//overlays:__subpackages__"],
)

argocd_app(
    name = "cert-manager",
    chart = "projects/platform/cert-manager",
    chart_files = ":chart",
    namespace = "cert-manager",
    release_name = "cert-manager",
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "values.yaml",
        "values-prod.yaml",
    ],
)

argocd_app(
    name = "cert-manager-gke",
    chart = "projects/platform/cert-manager",
    chart_files = ":chart",
    generate_semgrep = False,
    namespace = "cert-manager",
    release_name = "cert-manager",
    tags = [
        "helm",
        "template",
    ],
    target_suffix = "_gke",
    values_files = [
        "values.yaml",
    ],
)
