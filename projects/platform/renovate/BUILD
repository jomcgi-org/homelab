load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart")
load("//bazel/semgrep/defs:defs.bzl", "semgrep_test")
load("//bazel/tools/pytest:defs.bzl", "py_test")

helm_chart(
    name = "chart",
    visibility = ["//overlays:__subpackages__"],
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
    generate_semgrep = False,
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

# The generic Helm template target proves only that each overlay is valid YAML.
# This focused test pins the migration's single-writer staging contract across
# the chart, both cluster Applications, enrollment, and this build graph.
py_test(
    name = "migration_staging_test",
    srcs = ["migration_staging_test.py"],
    data = [
        "BUILD",
        ":chart",
        "//projects:renovate_enrollment_manifests",
        "//projects/platform-gke/renovate:manifests",
        "@multitool//tools/helm",
    ],
    env = {
        "HELM_BIN": "$(rootpath @multitool//tools/helm)",
    },
    deps = ["@pip//pyyaml"],
)

semgrep_test(
    name = "migration_staging_test_semgrep_test",
    srcs = ["migration_staging_test.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)
