load("//bazel/helm:defs.bzl", "argocd_app")

argocd_app(
    name = "cloudflare-gateway",
    chart = "projects/platform/cloudflare-gateway",
    chart_files = "//projects/platform/cloudflare-gateway:chart",
    namespace = "envoy-gateway-system",
    release_name = "cloudflare-gateway",
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "//projects/platform/cloudflare-gateway:values.yaml",
        "//projects/platform/cloudflare-gateway:values-gke.yaml",
    ],
)
