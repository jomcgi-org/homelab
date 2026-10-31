load("//bazel/helm:defs.bzl", "argocd_app")

argocd_app(
    name = "authentik",
    chart = "projects/platform/authentik",
    chart_files = "//projects/platform/authentik:chart",
    namespace = "authentik",
    release_name = "authentik",
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "//projects/platform/authentik:values.yaml",
        "//projects/platform/authentik:values-gke.yaml",
    ],
)
