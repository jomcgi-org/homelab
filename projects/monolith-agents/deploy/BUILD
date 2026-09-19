load("//bazel/helm:defs.bzl", "helm_admissibility_render")

filegroup(
    name = "deploy_values",
    srcs = ["values.yaml"],
    visibility = ["//projects/monolith-agents/chart:__pkg__"],
)

helm_admissibility_render(
    name = "render_manifests",
    chart = "projects/monolith-agents/chart",
    chart_files = "//projects/monolith-agents/chart:chart",
    namespace = "monolith-agents",
    release_name = "monolith-agents",
    values_files = [":deploy_values"],
)

helm_admissibility_render(
    name = "render_manifests_gke",
    chart = "projects/monolith-agents/chart",
    chart_files = "//projects/monolith-agents/chart:chart",
    namespace = "monolith-agents",
    release_name = "monolith-agents",
    target_suffix = "_gke",
    values_files = [
        ":deploy_values",
        ":gke_values",
    ],
)

filegroup(
    name = "gke_values",
    srcs = ["values-gke.yaml"],
    visibility = ["//projects/monolith-agents/chart:__pkg__"],
)

filegroup(
    name = "application",
    srcs = ["application.yaml"],
    visibility = ["//visibility:public"],
)

filegroup(
    name = "kustomization",
    srcs = ["kustomization.yaml"],
    visibility = ["//visibility:public"],
)
