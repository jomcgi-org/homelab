load("//bazel/helm:defs.bzl", "argocd_app")

argocd_app(
    name = "stars-forecast",
    chart = "projects/stars-forecast/chart",
    chart_files = "//projects/stars-forecast/chart:chart",
    content_rules = True,  # keep
    # The source chart intentionally refuses to render without a built image.
    # argocd_app's manual source-path renderer cannot resolve generated values.
    generate_manifests = False,  # keep
    namespace = "stars-forecast",
    release_name = "stars-forecast",
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "//projects/stars-forecast/chart:values.yaml",
        "//projects/stars-forecast/chart:image_values",
    ],  # keep
)
