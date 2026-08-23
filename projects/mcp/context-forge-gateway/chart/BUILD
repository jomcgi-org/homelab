load("//bazel/helm:defs.bzl", "helm_chart")
load("//bazel/semgrep/defs:defs.bzl", "semgrep_test")
load("//bazel/tools/pytest:defs.bzl", "py_test")

helm_chart(
    name = "chart",
    lint = False,
    visibility = ["//overlays:__subpackages__"],
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
