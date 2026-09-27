# gazelle:exclude reconcile_orphaned_iscsi.py
# gazelle:exclude reconcile_orphaned_iscsi_test.py

load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart")
load("//bazel/tools/pytest:defs.bzl", "py_test")

py_library(
    name = "reconcile_orphaned_iscsi_lib",
    srcs = ["reconcile_orphaned_iscsi.py"],
    imports = ["."],
)

py_test(
    name = "reconcile_orphaned_iscsi_test",
    srcs = ["reconcile_orphaned_iscsi_test.py"],
    deps = [
        ":reconcile_orphaned_iscsi_lib",
        "@pip//pytest",
    ],
)

helm_chart(
    name = "chart",
    lint = False,
    visibility = ["//overlays:__subpackages__"],
)

argocd_app(
    name = "longhorn",
    chart = "projects/platform/longhorn",
    chart_files = ":chart",
    namespace = "longhorn",
    release_name = "longhorn",
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "values.yaml",
        "values-prod.yaml",
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
