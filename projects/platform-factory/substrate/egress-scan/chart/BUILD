load("//bazel/helm:defs.bzl", "helm_chart", "helm_lint_test")
load("//bazel/tools/pytest:defs.bzl", "py_test")

helm_chart(
    name = "chart",
    images = {"image": "//projects/platform-factory/substrate/egress-scan:image.info"},
    lint = False,  # keep
    publish = True,
    visibility = ["//visibility:public"],
)

helm_lint_test(
    name = "lint_test",
    extra_values = [":chart.images_values"],
    tags = [
        "chart",
        "lint",
    ],
)

filegroup(
    name = "image_values",
    srcs = [":chart.images_values"],
    visibility = [
        "//projects/platform-factory/substrate/egress-scan/deploy:__pkg__",
        "//projects/platform-factory/substrate/resources:__pkg__",
    ],
)

# The chart defaults, read by resources_test to couple the gateway timeout to
# semgrep.deadline. A narrow target keeps chart_test.py out of that test.
filegroup(
    name = "values",
    srcs = ["values.yaml"],
    visibility = ["//projects/platform-factory/substrate/resources:__pkg__"],
)

py_test(
    name = "chart_test",
    srcs = ["chart_test.py"],
    data = [
        "Chart.yaml",
        "values.yaml",
        "@multitool//tools/helm",
    ] + glob(["templates/**/*"]),
    env = {"HELM_BIN": "$(rootpath @multitool//tools/helm)"},
    deps = ["@pip//pyyaml"],
)
