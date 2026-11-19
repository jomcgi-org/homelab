load("//bazel/helm:defs.bzl", "argocd_app")

exports_files(["application.yaml"])

argocd_app(
    name = "ships",
    chart = "projects/ships/chart",
    chart_files = "//projects/ships/chart:chart",
    content_rules = True,  # keep
    # The source chart intentionally refuses to render without a built image.
    # argocd_app's manual source-path renderer cannot resolve generated values.
    generate_manifests = False,  # keep
    namespace = "ships",
    release_name = "ships",
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "//projects/ships/chart:values.yaml",
        "//projects/ships/chart:image_values",
    ],  # keep
)
