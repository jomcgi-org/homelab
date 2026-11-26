load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart")
load("//bazel/tools/pytest:defs.bzl", "py_test")

exports_files(
    ["values-gke.yaml"],
    visibility = ["//projects/platform-gke/argocd:__pkg__"],
)

exports_files(
    ["values-factory.yaml"],
    visibility = ["//projects/platform-factory/argocd:__pkg__"],
)

helm_chart(
    name = "chart",
    lint = False,
    visibility = ["//visibility:public"],
)

argocd_app(
    name = "argocd",
    chart = "projects/platform/argocd",
    chart_files = ":chart",
    namespace = "argocd",
    release_name = "argocd",
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "values.yaml",
        "values-cluster.yaml",
    ],
)

argocd_app(
    name = "argocd-gke",
    chart = "projects/platform/argocd",
    chart_files = ":chart",
    namespace = "argocd",
    release_name = "argocd",
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

# Hand-written Helm regression test, as in platform/kargo.
py_test(
    name = "upgrade_test",
    srcs = ["upgrade_test.py"],
    data = [
        "Chart.lock",
        "Chart.yaml",
        "values.yaml",
        "values-factory.yaml",
        "values-gke.yaml",
        "@multitool//tools/helm",
    ] + glob([
        "templates/**/*",
        "charts/**/*",
    ]),
    env = {"HELM_BIN": "$(rootpath @multitool//tools/helm)"},
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)
