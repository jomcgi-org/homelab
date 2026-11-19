load("//bazel/helm:defs.bzl", "helm_chart")
load("//bazel/tools/pytest:defs.bzl", "py_test")

exports_files(["templates/db-secret.yaml"])

helm_chart(
    name = "chart",
    images = {"image": "//projects/trips/image:image.info"},
    lint = False,
    publish = True,
    visibility = ["//visibility:public"],
)

py_test(
    name = "trips_test",
    srcs = ["trips_test.py"],
    data = [
        "Chart.yaml",
        "values.yaml",
        "@multitool//tools/helm",
    ] + glob([
        "templates/**/*",
        "charts/*.tgz",
    ]),
    env = {"HELM_BIN": "$(rootpath @multitool//tools/helm)"},
    deps = ["@pip//pyyaml"],
)
