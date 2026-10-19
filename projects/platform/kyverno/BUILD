load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart")
load("//bazel/tools/pytest:defs.bzl", "py_test")

helm_chart(
    name = "chart",
    lint = False,
    visibility = ["//visibility:public"],
)

argocd_app(
    name = "kyverno",
    chart = "projects/platform/kyverno",
    chart_files = ":chart",
    namespace = "kyverno",
    release_name = "kyverno",
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "values.yaml",
    ],
)

# Render conditional workflow paths as well as the default admissibility inputs.
py_test(
    name = "policy_cleanup_test",
    srcs = ["policy_cleanup_test.py"],
    # Chart filegroups also contain other suites with their own runfiles/env.
    args = ["$(rootpath policy_cleanup_test.py)"],
    data = [
        ":chart",
        "//projects/monolith/chart",
        "//projects/platform/argo-workflows:chart",
        "//projects/platform/renovate:chart",
        "@multitool//tools/helm",
    ],
    env = {"HELM_BIN": "$(rootpath @multitool//tools/helm)"},
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
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
