load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart")

helm_chart(
    name = "chart",
    lint = False,
    visibility = ["//overlays:__subpackages__"],
)

argocd_app(
    name = "cloudflare-gateway",
    chart = "projects/platform/cloudflare-gateway",
    chart_files = ":chart",
    namespace = "envoy-gateway-system",
    release_name = "cloudflare-gateway",
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "values.yaml",
        "values-prod.yaml",
    ],
)

argocd_app(
    name = "cloudflare-gateway-gke",
    chart = "projects/platform/cloudflare-gateway",
    chart_files = ":chart",
    namespace = "envoy-gateway-system",
    release_name = "cloudflare-gateway",
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "values.yaml",
        "values-gke.yaml",
    ],
)
