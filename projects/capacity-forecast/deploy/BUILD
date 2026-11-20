load("//bazel/helm:defs.bzl", "argocd_app")

exports_files([
    "application.yaml",
    "kustomization.yaml",
])

argocd_app(
    name = "capacity-forecast",
    chart = "projects/capacity-forecast/chart",
    chart_files = "//projects/capacity-forecast/chart:chart",
    content_rules = True,  # keep
    generate_manifests = False,  # keep
    namespace = "capacity-forecast",
    release_name = "capacity-forecast",
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "//projects/capacity-forecast/chart:values.yaml",
        "//projects/capacity-forecast/chart:image_values",
    ],  # keep
)
