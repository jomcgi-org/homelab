load("//bazel/helm:defs.bzl", "argocd_app")

filegroup(
    name = "deploy_values",
    srcs = ["values.yaml"],
    visibility = ["//projects/mcp/context-forge-gateway/chart:__pkg__"],
)

filegroup(
    name = "gke_values",
    srcs = ["values-gke.yaml"],
    visibility = ["//projects/mcp/context-forge-gateway/chart:__pkg__"],
)

argocd_app(
    name = "context-forge-gateway",
    chart = "projects/mcp/context-forge-gateway/chart",
    chart_files = "//projects/mcp/context-forge-gateway/chart:chart",
    namespace = "mcp",
    release_name = "context-forge-gateway",
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "//projects/mcp/context-forge-gateway/chart:values.yaml",
        "values.yaml",
    ],
)
