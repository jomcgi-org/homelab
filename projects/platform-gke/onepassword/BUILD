load("//bazel/helm:defs.bzl", "argocd_app")

argocd_app(
    name = "onepassword",
    chart = "projects/platform/onepassword",
    chart_files = "//projects/platform/onepassword:chart",
    namespace = "default",
    release_name = "connect",
    tags = [
        "helm",
        "template",
    ],
    values_files = ["//projects/platform/onepassword:values.yaml"],
)
