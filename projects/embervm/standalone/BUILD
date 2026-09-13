# gazelle:ignore
# gazelle:exclude functions
load("//bazel/tools/pytest:defs.bzl", "py_test")

py_test(
    name = "quickstart_test",
    srcs = ["quickstart_test.py"],
    data = [
        "build-fixtures.py",
        "cleanup.sh",
        "functions/continuity/app.py",
        "host-check.sh",
        "install.sh",
        "platform.yaml",
        "upload-job.yaml",
        "values.yaml",
        "workloads/continuity.yaml",
        "workloads/hello.yaml",
        "//projects/embervm/chart:standalone_chart_sources",
        "@multitool//tools/helm",
    ],
    env = {
        "HELM_BIN": "$(rootpath @multitool//tools/helm)",
    },
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)
