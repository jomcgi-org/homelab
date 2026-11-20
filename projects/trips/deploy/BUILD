load("//bazel/helm:defs.bzl", "argocd_app")

argocd_app(
    name = "trips",
    chart = "projects/trips/chart",
    chart_files = "//projects/trips/chart:chart",
    content_rules = True,  # keep
    # The source chart refuses to render without a built image digest.
    generate_manifests = False,  # keep
    namespace = "trips",
    release_name = "trips",
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "//projects/trips/chart:values.yaml",
        "//projects/trips/chart:image_values",
    ],  # keep
)
