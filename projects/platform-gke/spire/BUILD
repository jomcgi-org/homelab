load("//bazel/helm:defs.bzl", "argocd_app")

argocd_app(
    name = "spire",
    chart = "projects/platform/spire",
    chart_files = "//projects/platform/spire:chart",
    namespace = "spire",
    release_name = "spire",
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "//projects/platform/spire:values.yaml",
        "//projects/platform/spire:values-gke.yaml",
    ],
)
