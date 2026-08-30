load("//bazel/helm:defs.bzl", "argocd_app", "helm_chart")
load("//bazel/semgrep/defs:defs.bzl", "semgrep_test")
load("//bazel/tools/pytest:defs.bzl", "py_test")

# This script is mounted from the Helm chart and executed with the image's
# python3, not as a Bazel binary. Keep it in this package so the test can load
# it as data without Gazelle creating a nested files/ package and a generated
# py_venv_binary that has no mandatory main attribute.
# gazelle:exclude files/reconcile_team_mapping.py

helm_chart(
    name = "chart",
    lint = False,
    visibility = ["//overlays:__subpackages__"],
)

argocd_app(
    name = "context-forge-gke",
    chart = "projects/mcp/context-forge-gateway/chart",
    chart_files = ":chart",
    generate_manifests = False,
    generate_semgrep = False,
    namespace = "mcp",
    release_name = "context-forge-gateway",
    tags = [
        "helm",
        "template",
    ],
    target_suffix = "_gke",
    values_files = [
        "values.yaml",
        "//projects/mcp/context-forge-gateway/deploy:deploy_values",
        "//projects/mcp/context-forge-gateway/deploy:gke_values",
    ],
)

# The team-mapping reconciler is shipped inside the chart (files/) and mounted
# from a ConfigMap, so it is not a Python package gazelle can see. Declare the
# test by hand, the way the embervm chart tests are.
py_test(
    name = "reconcile_team_mapping_test",
    srcs = ["reconcile_team_mapping_test.py"],
    data = ["files/reconcile_team_mapping.py"],
    env = {
        "RECONCILE_SCRIPT": "$(rootpath files/reconcile_team_mapping.py)",
    },
    deps = ["@pip//pytest"],
)

semgrep_test(
    name = "reconcile_team_mapping_test_semgrep_test",
    srcs = ["reconcile_team_mapping_test.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)
