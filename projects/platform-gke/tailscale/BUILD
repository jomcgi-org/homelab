load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart")

helm_chart(
    name = "chart",
    lint = False,
    visibility = ["//visibility:public"],
)

argocd_app(
    name = "tailscale",
    chart = "projects/platform-gke/tailscale",
    chart_files = ":chart",
    namespace = "tailscale",
    release_name = "tailscale",
    tags = [
        "helm",
        "template",
    ],
    values_files = ["values.yaml"],
)
