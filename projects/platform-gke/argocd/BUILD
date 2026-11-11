load("//bazel/helm:defs.bzl", "argocd_app")

argocd_app(
    name = "argocd",
    chart = "projects/platform/argocd",
    chart_files = "//projects/platform/argocd:chart",
    namespace = "argocd",
    release_name = "argocd",
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "//projects/platform/argocd:values.yaml",
        "//projects/platform/argocd:values-gke.yaml",
    ],
)
