load("//bazel/helm:defs.bzl", "argocd_app")

argocd_app(
    name = "otel-collector",
    chart = "projects/platform/otel-collector",
    chart_files = "//projects/platform/otel-collector:chart",
    namespace = "otel-collector",
    release_name = "otel-collector",
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "//projects/platform/otel-collector:values.yaml",
        "//projects/platform/otel-collector:values-gke.yaml",
    ],
)
