load("//bazel/helm:defs.bzl", "argocd_app")

argocd_app(
    name = "factory-storyboard",
    chart = "projects/factory-storyboard/chart",
    chart_files = "//projects/factory-storyboard/chart:chart",
    content_rules = True,  # keep
    # The source chart intentionally refuses to render without a built image.
    # argocd_app's manual source-path renderer cannot resolve generated values.
    generate_manifests = False,  # keep
    namespace = "factory",
    release_name = "factory-storyboard",
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "//projects/factory-storyboard/chart:values.yaml",
        "//projects/factory-storyboard/chart:image_values",
    ],  # keep
)
