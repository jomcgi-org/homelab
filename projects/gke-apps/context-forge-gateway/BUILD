load("//bazel/helm:defs.bzl", "argocd_app")

# Export the live hub policy for Loom's Application contract test. Gazelle owns
# the generated Application rules in this package.
exports_files(
    ["application.yaml"],
    visibility = ["//projects/loom/deploy:__pkg__"],
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
        "//projects/mcp/context-forge-gateway/deploy:values.yaml",
        "//projects/mcp/context-forge-gateway/deploy:values-gke.yaml",
    ],
)
