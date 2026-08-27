load("//bazel/helm:defs.bzl", "helm_admissibility_render")

# Rendered for //bazel/helm:chart_admissibility_test (#4831). Release name and
# namespace mirror deploy/application.yaml. helm loads the chart's own
# values.yaml implicitly before any --values overlay, so only the deploy
# overlay is listed here; that reproduces the Application's merge result.
helm_admissibility_render(
    name = "render_manifests",
    chart = "projects/mcp/context-forge-gateway/chart",
    chart_files = "//projects/mcp/context-forge-gateway/chart:chart",
    release_name = "context-forge-gateway",
    namespace = "mcp",
    values_files = ["values.yaml"],
)
