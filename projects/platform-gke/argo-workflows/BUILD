load("//bazel/helm:defs.bzl", "argocd_app")

argocd_app(
    name = "argo-workflows",
    chart = "projects/platform/argo-workflows",
    chart_files = "//projects/platform/argo-workflows:chart",
    namespace = "monolith-workflows",
    release_name = "argo-workflows",
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "//projects/platform/argo-workflows:values.yaml",
        "//projects/platform/argo-workflows:values-gke.yaml",
    ],
)
