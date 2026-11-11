load("//bazel/helm:defs.bzl", "argocd_app")

argocd_app(
    name = "priority-classes",
    chart = "projects/platform/priority-classes",
    chart_files = "//projects/platform/priority-classes:chart",
    namespace = "kube-system",
    release_name = "priority-classes",
    tags = [
        "helm",
        "template",
    ],
    values_files = ["//projects/platform/priority-classes:values.yaml"],
)
