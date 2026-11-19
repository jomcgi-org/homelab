load("//bazel/helm:defs.bzl", "argocd_app")

argocd_app(
    name = "factory-explore-site",
    chart = "projects/factory-explore-site/chart",
    chart_files = "//projects/factory-explore-site/chart:chart",
    content_rules = True,  # keep
    # The source chart intentionally refuses to render without a built image.
    # argocd_app's manual source-path renderer cannot resolve generated values.
    generate_manifests = False,  # keep
    namespace = "factory-explore-site",
    release_name = "factory-explore-site",
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "//projects/factory-explore-site/chart:values.yaml",
        "//projects/factory-explore-site/chart:image_values",
    ],  # keep
)
