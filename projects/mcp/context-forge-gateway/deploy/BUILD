# gazelle:argocd disabled
# The argocd gazelle extension would generate an argocd_app here (this is
# the one git-chart single-source app) whose template test
# depends on the chart target. The chart's visibility to this package is
# held by a gazelle keep marker in ../chart/BUILD, and the renders the
# admissibility gate consumes are the hand-written targets below.

load("//bazel/helm:defs.bzl", "helm_admissibility_render")

filegroup(
    name = "deploy_values",
    srcs = ["values.yaml"],
    visibility = ["//projects/mcp/context-forge-gateway/chart:__pkg__"],
)

helm_admissibility_render(
    name = "render_manifests",
    chart = "projects/mcp/context-forge-gateway/chart",
    chart_files = "//projects/mcp/context-forge-gateway/chart:chart",
    namespace = "mcp",
    release_name = "context-forge-gateway",
    values_files = [":deploy_values"],
)

helm_admissibility_render(
    name = "render_manifests_gke",
    chart = "projects/mcp/context-forge-gateway/chart",
    chart_files = "//projects/mcp/context-forge-gateway/chart:chart",
    namespace = "mcp",
    release_name = "context-forge-gateway",
    target_suffix = "_gke",
    values_files = [
        "//projects/mcp/context-forge-gateway/chart:values.yaml",
        ":deploy_values",
        ":gke_values",
    ],
)

filegroup(
    name = "gke_values",
    srcs = ["values-gke.yaml"],
    visibility = ["//projects/mcp/context-forge-gateway/chart:__pkg__"],
)
