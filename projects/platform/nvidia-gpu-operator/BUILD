load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart")

helm_chart(
    name = "chart",
    lint = False,
    visibility = ["//overlays:__subpackages__"],
)

argocd_app(
    name = "nvidia-gpu-operator",
    chart = "projects/platform/nvidia-gpu-operator",
    chart_files = ":chart",
    namespace = "gpu-operator",
    release_name = "nvidia-gpu-operator",
    tags = [
        "helm",
        "template",
    ],
    values_files = ["values.yaml"],
)
