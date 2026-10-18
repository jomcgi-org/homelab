# gazelle:argocd_generate_manifests false
# Generate the Application's template test. The hand-written admissibility
# targets below own both manifest outputs and their values-file contracts.

load("//bazel/helm:defs.bzl", "argocd_app", "helm_admissibility_render")

exports_files(
    [
        "BUILD",
        "application.yaml",
    ],
    visibility = ["//bazel/helm/gazelle:__pkg__"],
)

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

# Markdown read by the monolith doc-manifest genrules (#6446). See
# //projects/monolith:repo_docs_srcs.
filegroup(
    name = "repo_docs",
    srcs = glob(
        ["**/*.md"],
        allow_empty = True,
        exclude = ["**/node_modules/**"],
    ),
    visibility = ["//projects/monolith:__pkg__"],
)

argocd_app(
    name = "context-forge-gateway",
    chart = "projects/mcp/context-forge-gateway/chart",
    chart_files = "//projects/mcp/context-forge-gateway/chart:chart",
    generate_manifests = False,
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
