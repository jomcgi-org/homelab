load("//bazel/helm:defs.bzl", "argocd_app")

argocd_app(
    name = "polylane",
    chart = "projects/platform/polylane",
    chart_files = "//projects/platform/polylane:chart",
    namespace = "polylane",
    release_name = "polylane-k8s",
    tags = [
        "helm",
        "template",
    ],
    values_files = ["//projects/platform/polylane:values.yaml"],
)
