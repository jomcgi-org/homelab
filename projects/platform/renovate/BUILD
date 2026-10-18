load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart")
load("//bazel/tools/pytest:defs.bzl", "py_test")

helm_chart(
    name = "chart",
    visibility = ["//visibility:public"],
)

argocd_app(
    name = "renovate",
    chart = "projects/platform/renovate",
    chart_files = ":chart",
    namespace = "monolith-workflows",
    release_name = "renovate",
    tags = [
        "helm",
        "template",
    ],
    values_files = [
        "values.yaml",
    ],
)

argocd_app(
    name = "renovate-gke",
    chart = "projects/platform/renovate",
    chart_files = ":chart",
    namespace = "monolith-workflows",
    release_name = "renovate",
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

py_test(
    name = "dependency_policy_test",
    srcs = ["dependency_policy_test.py"],
    data = [
        ":chart",
        "//:renovate.json",
    ],
)

# Pin effective suspension, credential references and the hub enrollment path.
py_test(
    name = "hub_deployment_test",
    srcs = ["hub_deployment_test.py"],
    data = [
        "BUILD",
        ":chart",
        "//projects:hub_renovate_enrollment_manifests",
        "//projects/platform-gke/renovate:manifests",
        "@multitool//tools/helm",
        "@nodejs_linux_amd64//:node_bin",
    ],
    env = {
        "HELM_BIN": "$(rootpath @multitool//tools/helm)",
        "NODE_BIN": "$(rootpath @nodejs_linux_amd64//:node_bin)",
    },
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
