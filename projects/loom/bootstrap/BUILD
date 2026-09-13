load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart")

helm_chart(
    name = "chart",
    visibility = ["//overlays:__subpackages__"],
)

argocd_app(
    name = "loom-bootstrap",
    chart = "projects/loom/bootstrap",
    chart_files = ":chart",
    namespace = "monolith",
    release_name = "loom-bootstrap",
    tags = ["helm", "template"],
    values_files = ["values.yaml"],
)
