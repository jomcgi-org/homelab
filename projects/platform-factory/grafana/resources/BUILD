load("//bazel/tools/pytest:defs.bzl", "py_test")

# The runbook server ships in a ConfigMap, not as a Bazel binary. Keep
# Gazelle from making runbooks/ a package, which would break the labels below.
# gazelle:exclude runbooks

# provision_viewer.py ships in a ConfigMap and runs in the Grafana bootstrap
# container; tests/bootstrap.sh exercises it.
# gazelle:exclude provision_viewer.py

exports_files([
    "dashboards/capacity-advisor.json",
    "kustomization.yaml",
    "runbooks-route.yaml",
])

py_test(
    name = "semgrep_runbook_test",
    srcs = ["semgrep_runbook_test.py"],
    data = [
        "runbooks/SemgrepSyncFailed.json",
        "runbooks/server.py",
        "@promtool_3_15_1_linux_amd64//:binary",
    ],
    env = {"PROMTOOL": "$(rootpath @promtool_3_15_1_linux_amd64//:binary)"},
    target_compatible_with = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
    ],
)

py_test(
    name = "oauth_test",
    srcs = ["oauth_test.py"],
    data = ["grafana.ini"],
)

py_test(
    name = "runbook_actions_test",
    srcs = ["runbook_actions_test.py"],
    data = glob(["runbooks/*.json"]) + ["runbooks/server.py"],
)
