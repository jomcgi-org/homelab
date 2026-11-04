load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart")

exports_files(
    ["values-gke.yaml"],
    visibility = ["//projects/platform-gke/cloudflare-gateway:__pkg__"],
)

# Render and version pins for focused consumers that verify the gateway's
# trusted-origin boundary against the exact chart and CRD bundles deployed.
filegroup(
    name = "gateway_test_inputs",
    srcs = [
        "Chart.lock",
        "Chart.yaml",
        "values.yaml",
        "values-gke.yaml",
        "values-prod.yaml",
    ] + glob([
        "charts/**/*",
        "templates/**/*",
    ]),
    visibility = [
        "//projects/monolith:__pkg__",
        # The connector's chart test checks its route has a descriptor-cap entry.
        "//projects/scotland-connector/chart:__pkg__",
    ],
)

helm_chart(
    name = "chart",
    lint = False,
    visibility = ["//visibility:public"],
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
    target_suffix = "_gke",
    values_files = [
        "values.yaml",
        "values-gke.yaml",
    ],
)
