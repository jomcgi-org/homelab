load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart")
load("//bazel/tools/pytest:defs.bzl", "py_test")

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

# Hand-written Helm regression test, as in platform/kargo.
py_test(
    name = "upgrade_test",
    srcs = ["upgrade_test.py"],
    data = [
        "Chart.lock",
        "Chart.yaml",
        "values.yaml",
        "values-gke.yaml",
        "@multitool//tools/helm",
    ] + glob([
        "templates/**/*",
        "charts/**/*",
    ]),
    env = {"HELM_BIN": "$(rootpath @multitool//tools/helm)"},
    deps = ["@pip//pyyaml"],
)
