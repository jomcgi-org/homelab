load("//bazel/helm:defs.bzl", "helm_admissibility_render", "helm_chart")
load("//bazel/tools/pytest:defs.bzl", "py_test")

filegroup(
    name = "gke_values",
    srcs = ["values-gke.yaml"],
    visibility = ["//projects/inference:__subpackages__"],
)

helm_chart(
    name = "chart",
    visibility = ["//visibility:public"],
)

py_test(
    name = "chart_render_test",
    srcs = ["chart_render_test.py"],
    data = [
        ":chart",
        "@multitool//tools/helm",
    ],
    env = {"HELM_BIN": "$(rootpath @multitool//tools/helm)"},
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)

helm_admissibility_render(
    name = "render_manifests",
    chart = "projects/inference/deploy",
    chart_files = ":chart",
    namespace = "inference",
    release_name = "inference",
    values_files = ["values.yaml"],
)

helm_admissibility_render(
    name = "render_manifests_gke",
    chart = "projects/inference/deploy",
    chart_files = ":chart",
    namespace = "inference",
    release_name = "inference",
    target_suffix = "_gke",
    values_files = [
        "values.yaml",
        ":gke_values",
    ],
)
