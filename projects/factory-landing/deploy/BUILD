load("//bazel/helm:defs.bzl", "argocd_app")

argocd_app(
    name = "factory-landing",
    chart = "projects/factory-landing/chart",
    chart_files = "//projects/factory-landing/chart:chart",
    content_rules = True,  # keep
    # The source chart intentionally refuses to render without a built image.
    # argocd_app's manual source-path renderer cannot resolve generated values.
    generate_manifests = False,  # keep
    namespace = "factory-landing",
    release_name = "factory-landing",
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "//projects/factory-landing/chart:values.yaml",
        "//projects/factory-landing/chart:image_values",
    ],  # keep
)
