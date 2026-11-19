load("//bazel/helm:defs.bzl", "argocd_app")

argocd_app(
    name = "hikes",
    chart = "projects/hikes/chart",
    chart_files = "//projects/hikes/chart:chart",
    content_rules = True,  # keep
    # The source chart intentionally refuses to render without a built image.
    # argocd_app's manual source-path renderer cannot resolve generated values.
    generate_manifests = False,  # keep
    namespace = "hikes",
    release_name = "hikes",
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "//projects/hikes/chart:values.yaml",
        "//projects/hikes/chart:image_values",
    ],  # keep
)
