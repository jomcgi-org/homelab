load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart")
load("//bazel/tools/pytest:defs.bzl", "py_test")

helm_chart(
    name = "chart",
    lint = False,
    visibility = ["//overlays:__subpackages__"],
)

argocd_app(
    name = "kargo",
    chart = "projects/platform/kargo",
    chart_files = ":chart",
    namespace = "kargo",
    release_name = "kargo",
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "values.yaml",
    ],
)

# Hand-written test target (this directory has no gazelle-managed Python).
# argocd_app only proves the chart renders; nothing there reads the promotion
# steps. This pins the staged revision gate (#4745): off by default it changes
# nothing in the render, and on it places an http step after argocd-wait on
# the monolith prod Stage keyed on chart_version. The data glob materializes
# the chart as runfiles so helm can render it during the test.
py_test(
    name = "promotion_gate_test",
    srcs = ["promotion_gate_test.py"],
    data = [
        "Chart.lock",
        "Chart.yaml",
        "values.yaml",
        "@multitool//tools/helm",
    ] + glob([
        "templates/**/*",
        "charts/**/*",
    ]),
    env = {
        "HELM_BIN": "$(rootpath @multitool//tools/helm)",
    },
    deps = ["@pip//pyyaml"],
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
