load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart")

helm_chart(
    name = "chart",
    lint = False,
    visibility = ["//overlays:__subpackages__"],
)

argocd_app(
    name = "spire",
    chart = "projects/platform/spire",
    chart_files = ":chart",
    namespace = "spire",
    release_name = "spire",
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "values.yaml",
    ],
)

argocd_app(
    name = "spire-gke",
    chart = "projects/platform/spire",
    chart_files = ":chart",
    generate_manifests = False,
    generate_semgrep = False,
    namespace = "spire",
    release_name = "spire",
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
