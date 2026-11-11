load("//bazel/helm:defs.bzl", "argocd_app")

# Hand-maintained export for //projects:backup_status_application. The
# generated argocd_app rule does not export application.yaml, and globs cannot
# cross package boundaries, so the parent filegroup depends on this label.
exports_files(
    ["application.yaml"],
    visibility = ["//projects:__pkg__"],
)

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
