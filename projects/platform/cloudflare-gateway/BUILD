load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart")

# Schema pin for focused consumers that must stay within the installed Envoy
# Gateway API. This is the exact CRD bundle deployed by this chart.
filegroup(
    name = "gateway_crds",
    srcs = ["charts/gateway-crds-helm-1.8.3.tgz"],
    visibility = ["//projects/monolith:__pkg__"],
)

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
    generate_manifests = False,
    generate_semgrep = False,
    namespace = "envoy-gateway-system",
    release_name = "cloudflare-gateway",
    tags = [
        "helm",
        "template",
    ],
    target_suffix = "_gke",
    values_files = [
        "values.yaml",
        "values-gke.yaml",
    ],
)
