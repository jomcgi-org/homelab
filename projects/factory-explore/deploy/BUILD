load("//bazel/helm:defs.bzl", "argocd_app")

argocd_app(
    name = "factory-explore",
    chart = "projects/factory-explore/chart",
    chart_files = "//projects/factory-explore/chart:chart",
    # The source chart intentionally refuses to render without a built image.
    # argocd_app's manual source-path renderer cannot resolve generated values.
    generate_manifests = False,  # keep
    namespace = "factory-explore",
    release_name = "factory-explore",
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "//projects/factory-explore/chart:values.yaml",
        "//projects/factory-explore/chart:image_values",
    ],  # keep
)
