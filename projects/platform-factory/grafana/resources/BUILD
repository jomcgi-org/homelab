load("//bazel/tools/pytest:defs.bzl", "py_test")

exports_files(["runbooks-route.yaml"])

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
    deps = ["@pip//pytest"],
)
